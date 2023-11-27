# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# Copyright © 2023 aph5nt

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import os
import time
import torch
import argparse
import traceback
import bittensor as bt
from random import sample
from insights import protocol
from insights.protocol import MinerDiscoveryOutput
from neurons.nodes.nodes import get_node
from neurons.validators.miner_registry import MinerRegistryManager
from neurons.validators.scoring import build_miner_distribution, calculate_score, verify_data_sample


def get_config():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--alpha", default=0.9, type=float, help="The weight moving average scoring.py."
    )

    parser.add_argument(
        "--bitcoin_cheat_factor_sample_size",
        default=256,
        help="Bitcoin sample size used for calculating cheat factor.",
    )

    parser.add_argument("--netuid", type=int, default=15, help="The chain subnet uid.")

    bt.subtensor.add_args(parser)
    bt.logging.add_args(parser)
    bt.wallet.add_args(parser)

    config = bt.config(parser)
    config.full_path = os.path.expanduser(
        "{}/{}/{}/netuid{}/{}".format(
            config.logging.logging_dir,
            config.wallet.name,
            config.wallet.hotkey,
            config.netuid,
            "validator",
        )
    )

    if not os.path.exists(config.full_path):
        os.makedirs(config.full_path, exist_ok=True)
    return config


def main(config):
    bt.logging.info(f"Running validator with config: {config}")
    bt.logging.info("Setting up bittensor objects.")

    bt.logging(config=config, logging_dir=config.full_path)
    wallet = bt.wallet(config=config)
    subtensor = bt.subtensor(config=config)
    dendrite = bt.dendrite(wallet=wallet)
    metagraph = subtensor.metagraph(config.netuid)
    metagraph.sync(subtensor = subtensor)

    bt.logging.info(f"Wallet: {wallet}")
    bt.logging.info(f"Subtensor: {subtensor}")
    bt.logging.info(f"Dendrite: {dendrite}")
    bt.logging.info(f"Metagraph: {metagraph}")

    if wallet.hotkey.ss58_address not in metagraph.hotkeys:
        bt.logging.error(
            f"\nYour validator: {wallet} if not registered to chain connection: {subtensor} \nRun btcli register and try again."
        )
        exit()

    my_subnet_uid = metagraph.hotkeys.index(wallet.hotkey.ss58_address)
    bt.logging.info(f"Running validator on uid: {my_subnet_uid}")
    bt.logging.info("Building validation weights.")

    scores = torch.zeros_like(metagraph.S, dtype=torch.float32)
    scores = scores * (metagraph.total_stake < 1.024e3) # all nodes with more than 1e3 total stake are set to 0 (sets validators weights to 0)
    scores = scores * torch.Tensor([metagraph.neurons[uid].axon_info.ip != '0.0.0.0' for uid in metagraph.uids]) # set all nodes without ips set to 0
    bt.logging.info(f"Initial scores: {scores}")

    bt.logging.info("Starting validator loop.")
    step = 0

    while True:
        # Per 10 blocks, sync the subtensor state with the blockchain.
        if step % 5 == 0:
            bt.logging.info(f"🔄 Syncing metagraph with subtensor.")
            metagraph.sync(subtensor = subtensor)


        # If there are more uids than scores, add more weights.
        # Get the uids of all miners in the network.
        uids = metagraph.uids.tolist()
        if len(uids) > len(scores):
            bt.logging.trace("Adding more weights")
            size_difference = len(uids) - len(scores)
            new_scores = torch.zeros(size_difference, dtype=torch.float32)
            scores = torch.cat((scores, new_scores))
            del new_scores

        # If there are less uids than scores, remove some weights.
        queryable_uids = (metagraph.total_stake < 1.024e3)
        bt.logging.debug(f"queryable_uids:{queryable_uids}")

        # Remove the weights of miners that are not queryable.
        queryable_uids = queryable_uids * torch.Tensor([metagraph.neurons[uid].axon_info.ip != '0.0.0.0' for uid in uids])
        active_miners = torch.sum(queryable_uids)
        bt.logging.debug(f"active_miners_queryable_uids:{queryable_uids}")
        bt.logging.debug(f"active_miners:{active_miners}")

        # zip uids and queryable_uids, filter only the uids that are queryable, unzip, and get the uids
        zipped_uids = list(zip(uids, queryable_uids))
        filtered_uids = list(zip(*filter(lambda x: x[1], zipped_uids)))[0]
        bt.logging.debug(f"filtered_uids:{filtered_uids}")

        # dendrites_per_query should be 34% of the total number of queryable uids, rounded up to the nearest integer
        dendrites_per_query = max(1, int(0.34 * active_miners + 0.5))
        dendrites_to_query = sample( filtered_uids, min( dendrites_per_query, len(filtered_uids) ) )
        bt.logging.info(f"dendrites_to_query:{dendrites_to_query}")

        try:
            # Filter metagraph.axons by indices saved in dendrites_to_query list
            filtered_axons = [metagraph.axons[i] for i in dendrites_to_query]
            bt.logging.info(f"filtered_axons: {filtered_axons}")

            responses = dendrite.query(
                filtered_axons,
                protocol.MinerDiscovery(),
                deserialize=True,
                timeout = 120,
            )

            # Get miner distribution
            miner_distribution = build_miner_distribution(responses)

            if len(miner_distribution) == 0:
                bt.logging.info(f"No online miners found. Skipping.")
                time.sleep(bt.__blocktime__ * 5)
                continue

            # Cache dictionary
            block_height_cache = {}

            for index, response in enumerate(responses):
                bt.logging.debug(f"processing response: {response}")
                if response.output is None:
                    bt.logging.debug(f"Skipping response")
                    continue

                # Vars
                output: MinerDiscoveryOutput = response.output
                network = output.metadata.network
                model_type = output.metadata.model_type

                start_block_height = output.start_block_height
                last_block_height = output.block_height

                data_samples = output.data_samples
                axon_ip = response.axon.ip
                hot_key = response.axon.hotkey
                response_time = response.axon.process_time

                node = get_node(network)
                data_samples_are_valid = True

                for data_sample in data_samples:
                    block_data = node.get_block_by_height(data_sample['block_height'])
                    data_sample_is_valid = verify_data_sample(
                        network=network,
                        input_result=data_sample,
                        block_data=block_data
                    )
                    if not data_sample_is_valid:
                        return False

                if len(data_samples) < 10:
                    data_samples_are_valid = False

                bitcoin_cheat_factor_sample_size = int(config.bitcoin_cheat_factor_sample_size)
                cheat_factor = MinerRegistryManager().calculate_cheat_factor(hot_key=hot_key, network=network, model_type=model_type, sample_size=bitcoin_cheat_factor_sample_size)

                if network not in block_height_cache:
                    block_height_cache[network] = node.get_current_block_height()

                score = calculate_score(
                    network,
                    response_time,
                    start_block_height,
                    last_block_height,
                    block_height_cache[network],
                    miner_distribution,
                    data_samples_are_valid,
                    cheat_factor
                )

                bt.logging.info(f" =========================================== {hot_key} ===========================================")
                bt.logging.info(f"Start block height: {start_block_height}")
                bt.logging.info(f"Last block height: {last_block_height}")
                bt.logging.info(f"Data samples are valid: {data_samples_are_valid}")
                bt.logging.info(f"Cheat factor: {cheat_factor}")
                bt.logging.info(f"Score: {score}")

                scores[dendrites_to_query[index]] = config.alpha * scores[dendrites_to_query[index]] + (1 - config.alpha) * score

                MinerRegistryManager().store_miner_metadata(
                    ip_address=axon_ip,
                    hot_key=hot_key,
                    network=network,
                    model_type=model_type,
                    response_time=response_time,
                    score=scores[dendrites_to_query[index]],
                )

                MinerRegistryManager().store_miner_block_height(
                    hot_key=hot_key,
                    network=network,
                    model_type=model_type,
                    block_height=last_block_height,
                )

            ## While loop end
            bt.logging.info(f"Scoring response: {scores}")

            if (step + 1) % 10 == 0:
                weights = torch.nn.functional.normalize(scores, p=1.0, dim=0)
                bt.logging.info(f"Setting weights: {weights}")
                result = subtensor.set_weights(
                    netuid=config.netuid,
                    wallet=wallet,
                    uids=metagraph.uids,
                    weights=weights,
                    wait_for_inclusion=True,
                )
                if result:
                    bt.logging.success("Successfully set weights.")
                    time.sleep(bt.__blocktime__ * 101)
                else:
                    bt.logging.error("Failed to set weights.")

            step += 1
            metagraph = subtensor.metagraph(config.netuid)
            time.sleep(bt.__blocktime__)

        except RuntimeError as e:
            bt.logging.error(e)
            traceback.print_exc()

        except KeyboardInterrupt:
            bt.logging.success("Keyboard interrupt detected. Exiting validator.")
            exit()

if __name__ == "__main__":
    config = get_config()

    config.subtensor.network = "finney"
    config.wallet.hotkey = 'default'
    config.wallet.name = 'validator'
    config.netuid = 15
    import os
    os.environ["BITCOIN_NODE_RPC_URL"] = "http://daxtohujek446464:lubosztezhujek3446457@62.210.88.131:8332"

    main(config)
