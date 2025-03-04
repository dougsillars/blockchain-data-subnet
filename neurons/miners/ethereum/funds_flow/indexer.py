import os
import signal
import time
from threading import Thread
from math import floor
from neurons.setup_logger import setup_logger
from neurons.nodes.evm.ethereum.node import EthereumNode
from neurons.miners.ethereum.funds_flow.graph_creator import GraphCreator
from neurons.miners.ethereum.funds_flow.graph_indexer import GraphIndexer

# Global flag to signal shutdown
shutdown_flag = False
logger = setup_logger("EthereumIndexer")


def shutdown_handler(signum, frame):
    global shutdown_flag
    logger.info(
        "Shutdown signal received. Waiting for current indexing to complete before shutting down."
    )
    shutdown_flag = True

def index_blocks(_ethereum_node, _graph_creator, _graph_indexer, start_height):
    global shutdown_flag
    skip_blocks = 6 # Set the number of block confirmations

    while not shutdown_flag:
        current_block_height = _ethereum_node.get_current_block_height() - 6
        if current_block_height - skip_blocks < 0:
            logger.info(f"Waiting min {skip_blocks} for blocks to be mined.")
            time.sleep(10)
            continue

        if start_height > current_block_height:
            logger.info(
                f"Waiting for new blocks. Current height is {current_block_height}."
            )
            time.sleep(10)
            continue

        block_height = start_height
        while block_height <= current_block_height - skip_blocks:
            block = _ethereum_node.get_block_by_height(block_height)
            num_transactions = len(block["transactions"])
            if num_transactions == 0:
                block_height += 1
                continue

            start_time = time.time()
            in_memory_graph = _graph_creator.create_in_memory_graph_from_block(block)
            success = _graph_indexer.create_graph_focused_on_funds_flow(in_memory_graph)
            end_time = time.time()
            time_taken = end_time - start_time
            node_block_height = ethereum_node.get_current_block_height()
            progress = block_height / node_block_height * 100
            formatted_num_transactions = "{:>4}".format(num_transactions)
            formatted_time_taken = "{:6.2f}".format(time_taken)
            formatted_tps = "{:8.2f}".format(
                num_transactions / time_taken if time_taken > 0 else float("inf")
            )
            formatted_progress = "{:6.2f}".format(progress)

            # if time_taken > 0:
            #     logger.info(
            #         "Block {:>6}: Processed {} transactions in {} seconds {} TPS Progress: {}%".format(
            #             block_height,
            #             formatted_num_transactions,
            #             formatted_time_taken,
            #             formatted_tps,
            #             formatted_progress,
            #         )
            #     )
            # else:
            #     logger.info(
            #         "Block {:>6}: Processed {} transactions in 0.00 seconds (  Inf TPS). Progress: {}%".format(
            #             block_height, formatted_num_transactions, formatted_progress
            #         )
            #     )

            if success:
                logger.info("Finished Block - {}".format(block_height))
                block_height += 1

                # indexer flooding prevention
                threshold = int(os.getenv('BLOCK_PROCESSING_TRANSACTION_THRESHOLD', 500))
                if num_transactions > threshold:
                    delay = float(os.getenv('BLOCK_PROCESSING_DELAY', 1))
                    logger.info(f"Block tx count above {threshold}, slowing down indexing by {delay} seconds to prevent flooding.")
                    time.sleep(delay)

            else:
                logger.error(f"Failed to index block {block_height}.")
                time.sleep(30)

            if shutdown_flag:
                logger.info(f"Finished indexing block {block_height} before shutdown.")
                break

def index_blocks_by_last_height(thread_index, start, last, _ethereum_node, _graph_creator, _graph_indexer):
    global shutdown_flag
    print('new Thread started : thread number - {}'.format(thread_index + 1))
    skip_blocks = 6 # Set the number of block confirmations
    # calculate start_block_height & last_block_height by thread_index and thread_depth
    start_height = start
    last_height = last
    while not shutdown_flag:
        current_block_height = last_height - 6
        if current_block_height - skip_blocks < 0:
            logger.info(f"Waiting min {skip_blocks} for blocks to be mined.")
            time.sleep(10)
            continue

        if start_height > current_block_height:
            logger.info(
                f"Waiting for new blocks. Current height is {current_block_height}."
            )
            time.sleep(10)
            continue

        block_height = start_height
        while block_height <= current_block_height - skip_blocks:
            block = _ethereum_node.get_block_by_height(block_height)
            num_transactions = len(block["transactions"])
            if num_transactions == 0:
                block_height += 1
                continue
            start_time = time.time()
            in_memory_graph = _graph_creator.create_in_memory_graph_from_block(block)
            success = _graph_indexer.create_graph_focused_on_funds_flow(in_memory_graph)
            end_time = time.time()
            time_taken = end_time - start_time
            progress = (block_height - start_height) / (last_height - start_height) * 100
            formatted_num_transactions = "{:>4}".format(num_transactions)
            formatted_time_taken = "{:6.2f}".format(time_taken)
            formatted_tps = "{:8.2f}".format(
                num_transactions / time_taken if time_taken > 0 else float("inf")
            )
            formatted_progress = "{:6.2f}".format(progress)

            # if time_taken > 0:
            #     logger.info(
            #         "Thread Index {} Block {:>6}: Processed {} transactions in {} seconds {} TPS Progress: {}%".format(
            #             thread_index,
            #             block_height,
            #             formatted_num_transactions,
            #             formatted_time_taken,
            #             formatted_tps,
            #             formatted_progress,
            #         )
            #     )
            # else:
            #     logger.info(
            #         "Thread Index {} Block {:>6}: Processed {} transactions in 0.00 seconds (  Inf TPS). Progress: {}%".format(
            #             thread_index, block_height, formatted_num_transactions, formatted_progress
            #         )
            #     )

            if success:
                logger.info("Finished Block - {}".format(block_height))
                block_height += 1

                # indexer flooding prevention
                threshold = int(os.getenv('BLOCK_PROCESSING_TRANSACTION_THRESHOLD', 500))
                if num_transactions > threshold:
                    delay = float(os.getenv('BLOCK_PROCESSING_DELAY', 1))
                    logger.info(f"Block tx count above {threshold}, slowing down indexing by {delay} seconds to prevent flooding.")
                    time.sleep(delay)

            else:
                logger.error(f"Failed to index block {block_height}.")
                time.sleep(30)

            if shutdown_flag:
                logger.info(f"Finished indexing block {block_height} before shutdown.")
                break

# Register the shutdown handler for SIGINT and SIGTERM
signal.signal(signal.SIGINT, shutdown_handler)
signal.signal(signal.SIGTERM, shutdown_handler)

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    ethereum_node = EthereumNode()
    graph_creator = GraphCreator()
    graph_indexer = GraphIndexer()

    num_threads = 8 # set number of thread 8 by default
    num_thread_str = os.getenv('ETHEREUM_THREAD_CNT', None)

    if num_thread_str is not None:
        num_threads = int(num_thread_str)

    retry_delay = 60

    start_height = 0
    last_height = 0
    start_height_str = os.getenv('ETHEREUM_START_BLOCK_HEIGHT', None)
    last_height_str = os.getenv('ETHEREUM_LAST_BLOCK_HEIGHT', None)

    graph_last_block_height = graph_indexer.get_latest_block_number() + 1
    if start_height_str is not None:
        start_height = int(start_height_str)
        # if graph_last_block_height > start_height:
        #     start_height = graph_last_block_height
    else:
        start_height = graph_last_block_height

    current_block_height = ethereum_node.get_current_block_height()
    if last_height_str is not None:
        last_height = int(last_height_str)
        if current_block_height > last_height:
            last_height = current_block_height
    else:
        last_height = current_block_height
    
    thread_depth = floor((last_height - start_height) / num_threads)
    restHeight = (last_height - start_height) % num_threads

    threads = []
    
    logger.info("Starting indexer")
    logger.info(f"Starting from block height: {start_height}")
    logger.info(f"Current node block height: {last_height}")
    logger.info(f"Latest indexed block height: {graph_last_block_height}")
    # indexing all old historical tx
    graph_indexer.create_indexes()
    for i in range(num_threads):
        start = start_height + i * thread_depth
        last = start_height + (i + 1) * thread_depth - 1
        if i == num_threads - 1:
            last = start_height + (i + 1) * thread_depth + restHeight
        thread = Thread(target=index_blocks_by_last_height, args=(i, start, last, ethereum_node, graph_creator, graph_indexer))
        thread.start()

    # while threads're indexing old tx data
    # indexing recent blocks
    while True:
        try:
            logger.info("Creating indexes...")
            
            logger.info("Starting indexing blocks...")

            logger.info('-- Main thread is running for indexing recent blocks --')
            index_blocks(ethereum_node, graph_creator, graph_indexer, last_height + 1)
            
            break
        except Exception as e:
            ## traceback.print_exc()
            logger.error(f"Retry failed with error: {e}")
            logger.info(f"Retrying in {retry_delay} seconds...")
            time.sleep(retry_delay)
        finally:
            graph_indexer.close()
            logger.info("Indexer stopped")
