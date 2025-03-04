import os
from neurons.setup_logger import setup_logger
from neo4j import GraphDatabase


logger = setup_logger("GraphIndexer")


class GraphIndexer:
    def __init__(
        self,
        graph_db_url: str = None,
        graph_db_user: str = None,
        graph_db_password: str = None,
    ):
        if graph_db_url is None:
            self.graph_db_url = (
                os.environ.get("GRAPH_DB_URL") or "bolt://localhost:7687"
            )
        else:
            self.graph_db_url = graph_db_url

        if graph_db_user is None:
            self.graph_db_user = os.environ.get("GRAPH_DB_USER") or ""
        else:
            self.graph_db_user = graph_db_user

        if graph_db_password is None:
            self.graph_db_password = os.environ.get("GRAPH_DB_PASSWORD") or ""
        else:
            self.graph_db_password = graph_db_password

        self.driver = GraphDatabase.driver(
            self.graph_db_url,
            auth=(self.graph_db_user, self.graph_db_password),
        )

    def close(self):
        self.driver.close()

    def get_latest_block_number(self):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (t:Transaction)
                RETURN MAX(t.block_height) AS latest_block_height
                """
            )
            single_result = result.single()
            if single_result[0] is None:
               return 0

            return single_result[0]
        
    def get_min_block_number(self):
        with self.driver.session() as session:
            result = session.run(
                """
                MATCH (t:Transaction)
                RETURN MIN(t.block_height) AS min_block_height
                """
            )
            single_result = result.single()
            if single_result[0] is None:
               return 0

            return single_result[0]

    from decimal import getcontext

    # Set the precision high enough to handle satoshis for Bitcoin transactions
    getcontext().prec = 28

    def create_indexes(self):
        with self.driver.session() as session:
            # Fetch existing indexes
            existing_indexes = session.run("SHOW INDEX INFO")
            existing_index_set = set()
            for record in existing_indexes:
                label = record["label"]
                property = record["property"]
                index_name = f"{label}-{property}"
                if index_name:
                    existing_index_set.add(index_name)

            index_creation_statements = {
                "Transaction-tx_id": "CREATE INDEX ON :Transaction(tx_id);",
                "Transaction-block_height": "CREATE INDEX ON :Transaction(block_height);",
                "Transaction-out_total_amount": "CREATE INDEX ON :Transaction(out_total_amount)",
                "Address-address": "CREATE INDEX ON :Address(address);",
                "SENT-value_satoshi": "CREATE INDEX ON :SENT(value_satoshi)",
            }

            for index_name, statement in index_creation_statements.items():
                if index_name not in existing_index_set:
                    try:
                        logger.info(f"Creating index: {index_name}")
                        session.run(statement)
                    except Exception as e:
                        logger.error(f"An exception occurred while creating index {index_name}: {e}")

    def create_graph_focused_on_money_flow(self, in_memory_graph, _bitcoin_node, batch_size=8):
        block_node = in_memory_graph["block"]
        transactions = block_node.transactions

        with self.driver.session() as session:
            # Start a transaction
            transaction = session.begin_transaction()

            try:
                for i in range(0, len(transactions), batch_size):
                    batch_transactions = transactions[i : i + batch_size]

                    # Process all transactions, inputs, and outputs in the current batch
                    batch_txns = []
                    batch_inputs = []
                    batch_outputs = []
                    for tx in batch_transactions:
                        in_amount_by_address = {} # input amounts by address in satoshi
                        out_amount_by_address = {} # output amounts by address in satoshi
                        
                        for vin in tx.vins:
                            if vin.tx_id == 0:
                                continue
                            address, amount = _bitcoin_node.get_address_and_amount_by_txn_id_and_vout_id(vin.tx_id, str(vin.vout_id))
                            if address in in_amount_by_address:
                                in_amount_by_address[address] += amount
                            else:
                                in_amount_by_address[address] = amount

                        for vout in tx.vouts:
                            amount = vout.value_satoshi
                            address = vout.address
                            if vout.address in out_amount_by_address:
                                out_amount_by_address[address] += amount
                            else:
                                out_amount_by_address[address] = amount
                        
                        for address in in_amount_by_address.keys():
                            if in_amount_by_address[address] == 0:
                                continue
                            if address in out_amount_by_address and out_amount_by_address[address] != 0:
                                if in_amount_by_address[address] > out_amount_by_address[address]:
                                    in_amount_by_address[address] -= out_amount_by_address[address]
                                    out_amount_by_address[address] = 0
                                elif in_amount_by_address[address] < out_amount_by_address[address]:
                                    out_amount_by_address[address] -= in_amount_by_address[address]
                                    in_amount_by_address[address] = 0
                                else:
                                    in_amount_by_address[address] = 0
                                    out_amount_by_address[address] = 0
                        
                        input_addresses = [address for address in in_amount_by_address.keys() if in_amount_by_address[address] != 0]
                        output_addresses = [address for address in out_amount_by_address.keys() if out_amount_by_address[address] != 0]
                                    
                        in_total_amount = sum([in_amount_by_address[address] for address in input_addresses])
                        out_total_amount = sum([out_amount_by_address[address] for address in output_addresses])
                        
                        inputs = [{"address": address, "amount": in_amount_by_address[address], "tx_id": tx.tx_id } for address in input_addresses]
                        outputs = [{"address": address, "amount": out_amount_by_address[address], "tx_id": tx.tx_id } for address in output_addresses]

                        batch_txns.append({
                            "tx_id": tx.tx_id,
                            "in_total_amount": in_total_amount,
                            "out_total_amount": out_total_amount,
                            "timestamp": tx.timestamp,
                            "block_height": tx.block_height,
                            "is_coinbase": tx.is_coinbase,
                        })
                        batch_inputs += inputs
                        batch_outputs += outputs

                    transaction.run(
                        """
                        UNWIND $transactions AS tx
                        MERGE (t:Transaction {tx_id: tx.tx_id})
                        ON CREATE SET t.timestamp = tx.timestamp,
                                    t.in_total_amount = tx.in_total_amount,
                                    t.out_total_amount = tx.out_total_amount,
                                    t.timestamp = tx.timestamp,
                                    t.block_height = tx.block_height,
                                    t.is_coinbase = tx.is_coinbase
                        """,
                        transactions=batch_txns,
                    )
                    
                    transaction.run(
                        """
                        UNWIND $inputs AS input
                        MERGE (a:Address {address: input.address})
                        MERGE (t:Transaction {tx_id: input.tx_id})
                        CREATE (a)-[:SENT { value_satoshi: input.amount }]->(t)
                        """,
                        inputs=batch_inputs
                    )
                    
                    transaction.run(
                        """
                        UNWIND $outputs AS output
                        MERGE (a:Address {address: output.address})
                        MERGE (t:Transaction {tx_id: output.tx_id})
                        CREATE (t)-[:SENT { value_satoshi: output.amount }]->(a)
                        """,
                        outputs=batch_outputs
                    )

                transaction.commit()
                return True

            except Exception as e:
                transaction.rollback()
                logger.error(f"An exception occurred: {e}")
                return False

            finally:
                if transaction.closed() is False:
                    transaction.close()
