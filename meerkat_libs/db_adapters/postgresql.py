import psycopg2
import logging
import json
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
from psycopg2 import sql
from psycopg2.extras import Json


class PostgreSQLAdapter():

    def __init__(self, connection_dsn, root_connection_dsn, structure):
        self.structure = structure
        self.connection_dsn = connection_dsn
        self.root_connection_dsn = root_connection_dsn
        self.keys = self._extract_keys()

    def drop_all_tables(self):
        try:
            logging.info("Dropping tables")
            conn = psycopg2.connect(self.connection_dsn)
            for table in self.structure.keys():
                conn.cursor().execute("DROP TABLE {};".format(table))
                conn.commit()
            conn.close()

        except psycopg2.ProgrammingError:
            logging.info("No tables exists to drop.")

    def setup(self):
        self._create_db_if_needed()
        self._create_tables_if_needed()

    def connect_to_db(self):
        logging.info("Trying to connect to the DB.")
        self.conn = psycopg2.connect(self.connection_dsn)
        logging.info("Connection esablished.")

    def _extract_keys(self):
        keys = {}
        for table, structure in self.structure.items():
            keys[table] = [x[0] for x in structure if x[0] != 'data']
        return keys

    def _create_db_if_needed(self):
        try:
            self.connect_to_db()
        except psycopg2.OperationalError:
            logging.info("Failed to connect. DB needs to be created.")
            self._create_db()

    def _create_db(self):
        logging.info("Creating the DB")
        conn = psycopg2.connect(self.root_connection_dsn)
        conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
        conn.cursor().execute("CREATE DATABASE "+self.db_name)
        conn.commit()
        conn.close()
        self.connect_to_db()

    def _create_tables_if_needed(self):
        for table_name, structure in self.structure.items():
            # Only create non-existant tables, so first check if table exists.
            try:
                logging.info("Checking if table {} exists.".format(table_name))
                cur = self.conn.cursor()
                cur.execute(sql.SQL("SELECT * FROM {} LIMIT 1;").format(
                    sql.Identifier(table_name)  # Always protect constructed SQL
                ))
                logging.info("Table {} exists.".format(table_name))

            except psycopg2.ProgrammingError:
                self._create_table(table_name, structure)

    def _create_table(self, table_name, structure):
        logging.info("Table {} needs to be created.".format(table_name))
        # Construct secure SQL query.
        structure = sql.SQL(", ").join(map(lambda x: x[1], structure))
        query = sql.SQL("CREATE TABLE {} ({});").format(
            sql.Identifier(table_name),
            structure
        )
        logging.debug("Create query: ".format(query.as_string(self.conn)))

        # Create a cursor and execute the table creation DB query.
        self.conn.rollback()
        cur = self.conn.cursor()
        cur.execute(query)
        self.conn.commit()

        logging.info("Table {} created".format(table_name))

    def read(self, table, key, attributes=[]):
        # Securely SQL stringify the attributes
        if attributes:
            tmp = [sql.SQL("data->>{}").format(sql.Literal(a)) for a in attributes]
            attributes = sql.SQL(", ").join(tmp)
        else:
            attributes = sql.SQL("data")

        # Securely SQL stringify the conditions
        conds = []
        for k, v in key.items():
            conds += [sql.SQL("{}={}").format(sql.Identifier(k), sql.Literal(v))]
        conditions = sql.SQL(" AND ").join(conds)

        # Form the secure SQL query
        query = sql.SQL("SELECT {} from {} where {};").format(
            attributes,
            sql.Identifier(table),
            conditions
        )
        logging.debug("Read query: {}".format(query.as_string(self.conn)))

        # Get and return the db result if it exists.
        # Function only searches on keys so should always be single result
        cur = self.conn.cursor()
        cur.execute(query)
        result = cur.fetchone()
        cur.close()
        if result:
            return result[0]
        else:
            return None  # The result may not exist

    def write(self, table, key, attributes):
        # Add the key data to the json blob.
        attributes = {**key, **attributes}
        # Stringify the insert data
        data = {**key, 'data': Json(attributes)}
        values = sql.SQL(", ").join(map(
            lambda x: sql.Literal(data[x[0]]),
            self.structure[table]
        ))

        # Securely build sql query to avoid sql injection.
        query = sql.SQL("INSERT INTO {} VALUES ({})").format(
            sql.Identifier(table),
            values
        )
        # Write should UPSERT i.e. it should overwrite if key already exists.
        updates = sql.SQL("{}.data").format(sql.Identifier(table))
        for attribute, value in attributes.items():
            updates = sql.SQL("jsonb_set({}, '{{{}}}', {})").format(
                updates,
                sql.Identifier(attribute),
                sql.Literal(json.dumps(value))
            )
        query += sql.SQL(" ON CONFLICT ({}) DO UPDATE SET data={};").format(
            sql.SQL(", ").join([sql.Identifier(x) for x in self.keys[table]]),
            updates
        )
        logging.debug("Write query: {}".format(query.as_string(self.conn)))

        # Insert the data
        cur = self.conn.cursor()
        cur.execute(query)
        self.conn.commit()
        cur.close()

    def delete(self, table, key):
        # Securely SQL stringify the conditions
        conditions = sql.SQL(" AND ").join(map(
            lambda k, v: sql.SQL("{}={}").format(sql.Identifier(k), sql.Literal(v)),
            key.items()
        ))

        # Securely build sql query to avoid sql injection.
        query = sql.SQL("DELETE from {} where {};").format(
            sql.Identifier(table),
            conditions
        )
        logging.debug("Delete query: {}".format(query.as_string(self.conn)))

        # Execute the delete query
        cur = self.conn.cursor()
        cur.execute(query)
        self.conn.commit()
        cur.close()

    def get_all(self, table_name, filters={}, attributes=[]):
        # Securely compose sql list of attributes.
        if attributes:
            attributes = sql.SQL(", ").join(map(
                lambda x: sql.SQL("data->>{}").format(sql.Literal(x)),
                attributes
            ))
        else:
            attributes = sql.SQL("data")

        sql_conditions = []

        # Convert filters securly into SQL string
        if filters:
            for field, values in filters.items():
                for value in values:
                    sql_conditions.append(
                        sql.SQL("data#>'{{{}}}' ? {}").format(
                            sql.SQL(field),
                            sql.Literal(value)
                        )
                    )
            sql_conditions = sql.SQL(' OR ').join(sql_conditions)
            sql_conditions = sql.SQL(' WHERE ') + sql_conditions

        else:
            sql_conditions = sql.SQL("")

        # Securely build sql query to avoid sql injection.
        query = sql.SQL("SELECT {} from {}{};").format(
            attributes,
            sql.Identifier(table_name),
            sql_conditions
        )
        logging.debug("Read query: {}".format(query.as_string(self.conn)))

        # Execute and fetch only JSON data as a list of dicts
        # (Don't need key data as it is merged into JSON data during db write.)
        cur = self.conn.cursor()
        cur.execute(query)
        results = cur.fetchall()
        results = [r[0] for r in results]
        cur.close()
        return results
