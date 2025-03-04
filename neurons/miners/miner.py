import argparse
import os
import time
import typing
import traceback
from random import sample

import yaml

import bittensor as bt

from insights import protocol


# import base miner class which takes care of most of the boilerplate
from template.base.miner import BaseMinerNeuron

from neurons.miners import blacklist
from insights.protocol import MODEL_TYPE_FUNDS_FLOW, NETWORK_BITCOIN
from neurons.storage import store_miner_metadata
from neurons.remote_config import MinerConfig
from neurons.nodes.factory import NodeFactory
from neurons.miners.query import get_graph_search, get_graph_indexer


class Miner(BaseMinerNeuron):
    """
    Your miner neuron class. You should use this class to define your miner's behavior. In particular, you should replace the forward function with your own logic. You may also want to override the blacklist and priority functions according to your needs.

    This class inherits from the BaseMinerNeuron class, which in turn inherits from BaseNeuron. The BaseNeuron class takes care of routine tasks such as setting up wallet, subtensor, metagraph, logging directory, parsing config, etc. You can override any of the methods in BaseNeuron if you need to customize the behavior.

    This class provides reasonable default behavior for a miner such as blacklisting unrecognized hotkeys, prioritizing requests based on stake, and forwarding requests to the forward function. If you need to define custom
    """
    @staticmethod
    def get_config():

        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--network",
            default=NETWORK_BITCOIN,
            help="Set miner's supported blockchain network.",
        )
        parser.add_argument(
            "--model_type",
            type=str,
            default=MODEL_TYPE_FUNDS_FLOW,
            help="Set miner's supported model type.",
        )

        parser.add_argument("--netuid", type=int, default=15, help="The chain subnet uid.")
        parser.add_argument("--dev", action=argparse.BooleanOptionalAction)

        
        bt.subtensor.add_args(parser)
        bt.logging.add_args(parser)
        bt.wallet.add_args(parser)
        bt.axon.add_args(parser)

        config = bt.config(parser)
        config.blacklist  = dict(force_validator_permit=True, allow_non_registered=False)
        config.wait_for_sync = os.environ.get('WAIT_FOR_SYNC', 'False')=='True'
        config.graph_db_url = os.environ.get('GRAPH_DB_URL', 'bolt://localhost:7687')
        config.graph_db_user = os.environ.get('GRAPH_DB_USER', 'user')
        config.graph_db_password = os.environ.get('GRAPH_DB_PASSWORD', 'pwd')
        
        dev = config.dev
        if dev:
            dev_config_path = "miner.yml"
            if os.path.exists(dev_config_path):
                with open(dev_config_path, 'r') as f:
                    dev_config = yaml.safe_load(f.read())
                config.update(dev_config)
                bt.logging.info(f"config updated with {dev_config_path}")

            else:
                with open(dev_config_path, 'w') as f:
                    yaml.safe_dump(config, f)
                bt.logging.info(f"config stored in {dev_config_path}")

        return config
    
    def __init__(self, config=None):
        config = Miner.get_config()
        
        super(Miner, self).__init__(config=config)
        

        self.request_timestamps: dict = {}
        
        self.axon = bt.axon(wallet=self.wallet, port=self.config.axon.port)        
        # Attach determiners which functions are called when servicing a request.
        bt.logging.info(f"Attaching forwards functions to miner axon.")
        self.axon.attach(
            forward_fn=self.block_check,
            blacklist_fn=self.block_check_blacklist,
            priority_fn=self.block_check_priority,
        ).attach(
            forward_fn=self.discovery,
            blacklist_fn=self.discovery_blacklist,
            priority_fn=self.discovery_priority,
        ).attach(
            forward_fn=self.query,
            blacklist_fn=self.query_blacklist,
            priority_fn=self.query_priority,
        )

        #to_remove_after_merge:
        self.axon.attach(
            forward_fn=self.deprecated_block_check,
            blacklist_fn=self.deprecated_block_check_blacklist,
            priority_fn=self.deprecated_block_check_priority,
        ).attach(
            forward_fn=self.deprecated_discovery,
            blacklist_fn=self.deprecated_discovery_blacklist,
            priority_fn=self.deprecated_discovery_priority,
        ).attach(
            forward_fn=self.deprecated_query,
            blacklist_fn=self.deprecated_query_blacklist,
            priority_fn=self.deprecated_query_priority,
        )

        bt.logging.info(f"Axon created: {self.axon}")

        self.graph_search = get_graph_search(config)

        self.miner_config = MinerConfig().load_and_get_config_values()        


    
    async def block_check(self, synapse: protocol.BlockCheck) -> protocol.BlockCheck:
        try:
            block_heights = synapse.blocks_to_check
            data_samples = self.graph_search.get_block_transactions(block_heights)
            synapse.output = protocol.BlockCheckOutput(
                data_samples=data_samples,
            )
            bt.logging.info(f"Serving miner random block check output: {synapse.output}")
        except Exception as e:
            bt.logging.error(traceback.format_exc())
            synapse.output = None
        return synapse
            
    async def discovery(self, synapse: protocol.Discovery ) -> protocol.Discovery:
        try:
            block_range = self.graph_search.get_block_range()
            start_block = block_range['start_block_height']
            last_block = block_range['latest_block_height']
            run_id = self.graph_search.get_run_id()

            synapse.output = protocol.DiscoveryOutput(
                metadata=protocol.DiscoveryMetadata(
                    network=self.config.network,
                    model_type=self.config.model_type,
                ),
                start_block_height=start_block,
                block_height=last_block,
                run_id=run_id,
            )
            bt.logging.info(f"Serving miner discovery output: {synapse.output}")
        except Exception as e:
            bt.logging.error(traceback.format_exc())
            synapse.output = None
        return synapse

    async def query(self, synapse: protocol.Query ) -> protocol.Query:
        try:
            synapse.output = self.graph_search.execute_query(
                network=synapse.network, query=synapse.query)
        except Exception as e:
            bt.logging.error(traceback.format_exc())
            synapse.output = None
        return synapse

    async def block_check_blacklist(self, synapse: protocol.BlockCheck) -> typing.Tuple[bool, str]:
        return blacklist.base_blacklist(self, synapse=synapse)

    async def discovery_blacklist(self, synapse: protocol.Discovery) -> typing.Tuple[bool, str]:
        return blacklist.discovery_blacklist(self, synapse=synapse)

    async def query_blacklist(self, synapse: protocol.Query) -> typing.Tuple[bool, str]:
        return blacklist.query_blacklist(self, synapse=synapse)


    def base_priority(self, synapse: bt.Synapse) -> float:
        caller_uid = self.metagraph.hotkeys.index(
            synapse.dendrite.hotkey
        ) 
        prirority = float(
            self.metagraph.S[caller_uid]
        )
        bt.logging.trace(
            f"Prioritizing {synapse.dendrite.hotkey} with value: ", prirority
        )
        return prirority
    
    async def block_check_priority(self, synapse: protocol.BlockCheck) -> float:
        return self.base_priority(synapse=synapse)

    async def discovery_priority(self, synapse: protocol.Discovery) -> float:
        return self.base_priority(synapse=synapse)

    async def query_priority(self, synapse: protocol.Query) -> float:
        return self.base_priority(synapse=synapse)

    def resync_metagraph(self):
        super(Miner, self).resync_metagraph()
        store_miner_metadata(self.config, self.graph_search, self.wallet)

    def save_state(self):
        #empty function to remove logging WARNING
        pass

    ### TO REMOVE AFTER MERGE WITH MAIN
    async def deprecated_block_check_priority(self, synapse: protocol.MinerRandomBlockCheck) -> float:
        return self.base_priority(synapse=synapse)

    async def deprecated_discovery_priority(self, synapse: protocol.MinerDiscovery) -> float:
        return self.base_priority(synapse=synapse)

    async def deprecated_query_priority(self, synapse: protocol.MinerQuery) -> float:
        return self.base_priority(synapse=synapse)
    
    async def deprecated_block_check_blacklist(self, synapse: protocol.MinerRandomBlockCheck) -> typing.Tuple[bool, str]:
        return blacklist.base_blacklist(self, synapse=synapse)

    async def deprecated_discovery_blacklist(self, synapse: protocol.MinerDiscovery) -> typing.Tuple[bool, str]:
        return blacklist.discovery_blacklist(self, synapse=synapse)

    async def deprecated_query_blacklist(self, synapse: protocol.MinerQuery) -> typing.Tuple[bool, str]:
        return blacklist.query_blacklist(self, synapse=synapse)

    async def deprecated_block_check(self, synapse: protocol.MinerRandomBlockCheck) -> protocol.MinerRandomBlockCheck:
        try:
            block_heights = synapse.blocks_to_check
            data_samples = self.graph_search.get_block_transactions(block_heights)
            synapse.output = protocol.MinerRandomBlockCheckOutput(
                data_samples=data_samples,
            )
            bt.logging.info(f"Serving miner random block check output: {synapse.output}")
        except Exception as e:
            bt.logging.error(traceback.format_exc())
            synapse.output = None
        return synapse
            
    async def deprecated_discovery(self, synapse: protocol.MinerDiscovery ) -> protocol.MinerDiscovery:
        try:
            block_range = self.graph_search.get_block_range()
            start_block = block_range['start_block_height']
            last_block = block_range['latest_block_height']
            run_id = self.graph_search.get_run_id()
            block_heights = sample(range(start_block, last_block + 1), 10)
            data_samples = self.graph_search.get_block_transactions(block_heights)

            synapse.output = protocol.MinerDiscoveryOutput(
                metadata=protocol.MinerDiscoveryMetadata(
                    network=self.config.network,
                    model_type=self.config.model_type,
                ),
                start_block_height=start_block,
                block_height=last_block,
                data_samples=data_samples,
                run_id=run_id,
                version=4
            )
            bt.logging.info(f"Serving miner discovery output: {synapse.output}")
        except Exception as e:
            bt.logging.error(traceback.format_exc())
            synapse.output = None
        return synapse

    async def deprecated_query(self, synapse: protocol.MinerQuery ) -> protocol.MinerQuery:
        try:
            synapse.output = self.graph_search.execute_query(
                network=synapse.network, query=synapse.query)
        except Exception as e:
            bt.logging.error(traceback.format_exc())
            synapse.output = None
        return synapse


def wait_for_blocks_sync():
        is_synced=False

        config = Miner.get_config()
        if not config.wait_for_sync:
            bt.logging.info(f"Skipping graph sync.")
            return is_synced
        
        miner_config = MinerConfig().load_and_get_config_values()
        delta = miner_config.get_blockchain_sync_delta(config.network)
        bt.logging.info(f"Waiting for graph model to sync with blockchain.")
        while not is_synced:
            try:
                graph_indexer = get_graph_indexer(config)
                node = NodeFactory.create_node(config.network)

                latest_block_height = node.get_current_block_height()
                current_block_height = graph_indexer.get_latest_block_number()
                delta = latest_block_height - current_block_height
                if delta < 100:
                    is_synced = True
                    bt.logging.success(f"Graph model is synced with blockchain.")
                else:
                    bt.logging.info(f"Graph Sync: {current_block_height}/{latest_block_height}")
                    time.sleep(bt.__blocktime__ * 12)
            except Exception as e:
                bt.logging.error(traceback.format_exc())
                time.sleep(bt.__blocktime__ * 12)
                bt.logging.info(f"Failed to connect with graph database. Retrying...")
                continue
        return is_synced

# This is the main function, which runs the miner.
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    wait_for_blocks_sync()
    with Miner() as miner:
        while True:
            bt.logging.info(f"Miner running")
            time.sleep(bt.__blocktime__*2)

