import re

# from snowflake.snowpark import Session
import snowflake.connector

from snowflake.connector.errors import ProgrammingError

from .client import get_session
from .identifiers import URN, FQN
from .resources import AccountGrant, Resource, Role, RoleGrant

# programming_error = re.Pattern(".*ProgrammingError.*")

# def _parse_programming_error(err):


ACCESS_CONTROL_ERR = 3001


def _fail_if_not_granted(result):
    if len(result) == 0:
        raise Exception("Failed to create grant")
    if len(result) == 1 and result[0]["status"] == "Grant not executed: Insufficient privileges.":
        raise Exception(result[0]["status"])


def execute(session, sql, use_role=None) -> list:
    with session.cursor(snowflake.connector.DictCursor) as cur:
        try:
            if use_role:
                cur.execute(f"USE ROLE {use_role}")
            print(f"[{session.role}] >>>", sql)
            result = cur.execute(sql).fetchall()
            return result
        except ProgrammingError as err:
            # if err.errno == ACCESS_CONTROL_ERR:
            #     raise Exception(f"Access control error: {err.msg}")
            raise ProgrammingError(f"failed to execute sql, [{sql}]", errno=err.errno) from err


def params_result_to_dict(params_result):
    params = {}
    for param in params_result:
        if param["type"] == "BOOLEAN":
            typed_value = param["value"] == "true"
        elif param["type"] == "NUMBER":
            typed_value = int(param["value"])
        elif param["type"] == "STRING":
            typed_value = str(param["value"]) if param["value"] else None
        params[param["key"].lower()] = typed_value
    return params


def options_result_to_list(options_result):
    return [option.strip(" ") for option in options_result.split(",")]


def remove_none_values(d):
    return {k: v for k, v in d.items() if v is not None}


def fetch_remote_state(provider: "DataProvider", manifest):
    state = {}
    for urn_str in manifest.keys():
        urn = URN.from_str(urn_str)
        data = provider.fetch_resource(urn)
        if data:
            state[urn_str] = remove_none_values(data)

    return state


class DataProvider:
    def __init__(self, session=None):
        self.session = session or get_session()

    # region Resource Fetching

    def fetch_resource(self, urn):
        return getattr(self, f"fetch_{urn.resource_key}")(urn.fqn)

    def fetch_account_locator(self):
        locator = execute(self.session, "SELECT CURRENT_ACCOUNT()")[0]
        return locator

    def fetch_region(self):
        region = execute(self.session, "SELECT CURRENT_REGION()")[0]
        return region

    def fetch_columns(self, resource_type: str, fqn: FQN):
        desc_result = execute(self.session, f"DESC {resource_type} {fqn}")
        columns = []
        for col in desc_result:
            if col["kind"] != "COLUMN":
                raise Exception(f"Unexpected kind {col['kind']} in desc result")
            columns.append(
                {
                    "name": col["name"],
                    "type": col["type"],
                    "nullable": col["null?"] == "Y",
                    "default": col["default"],
                    "comment": col["comment"],
                }
            )
        return columns

    def fetch_database(self, fqn: FQN):
        show_result = execute(self.session, f"SHOW DATABASES LIKE '{fqn.name}'")

        if len(show_result) == 0:
            return None
        if len(show_result) > 1:
            raise Exception(f"Found multiple databases matching {fqn}")
        if show_result[0]["kind"] != "STANDARD":
            return None

        options = options_result_to_list(show_result[0]["options"])
        show_params_result = execute(self.session, f"SHOW PARAMETERS IN DATABASE {fqn.name}")
        params = params_result_to_dict(show_params_result)

        return {
            "name": show_result[0]["name"],
            "data_retention_time_in_days": int(show_result[0]["retention_time"]),
            "comment": show_result[0]["comment"] or None,
            "transient": "TRANSIENT" in options,
            "owner": show_result[0]["owner"],
            "max_data_extension_time_in_days": params["max_data_extension_time_in_days"],
            "default_ddl_collation": params["default_ddl_collation"],
        }

    def fetch_account_grant(self, fqn):
        show_result = execute(self.session, "SHOW GRANTS ON ACCOUNT")
        role_account_grants = [row for row in show_result if row["grantee_name"] == fqn.name]

        if len(role_account_grants) == 0:
            return None

        return {
            "privs": sorted([row["privilege"] for row in role_account_grants]),
            "on": "ACCOUNT",
            "to": fqn.name,
        }

    def fetch_priv_grant(self, fqn):
        raise NotImplementedError

    def fetch_role(self, fqn):
        try:
            show_result = execute(self.session, f"SHOW ROLES LIKE '{fqn.name}'")
        except ProgrammingError:
            return None

        if len(show_result) == 0:
            return None
        if len(show_result) > 1:
            raise Exception(f"Found multiple roles matching {fqn}")

        data = show_result[0]

        return {
            "name": data["name"],
            "comment": data["comment"] or None,
            "owner": data["owner"],
        }

    def fetch_schema(self, fqn):
        if fqn.database is None:
            raise Exception(f"Schema fqn must have a database {fqn}")
        try:
            show_result = execute(self.session, f"SHOW SCHEMAS LIKE '{fqn.name}' IN DATABASE {fqn.database}")
        except ProgrammingError:
            return None

        if len(show_result) == 0:
            return None
        if len(show_result) > 1:
            raise Exception(f"Found multiple schemas matching {fqn}")

        data = show_result[0]

        options = options_result_to_list(data["options"])
        show_params_result = execute(self.session, f"SHOW PARAMETERS IN SCHEMA {fqn}")
        params = params_result_to_dict(show_params_result)

        return {
            "name": data["name"],
            "transient": "TRANSIENT" in options,
            "owner": data["owner"],
            "with_managed_access": "MANAGED ACCESS" in options,
            "data_retention_time_in_days": int(data["retention_time"]),
            "max_data_extension_time_in_days": params["data_retention_time_in_days"],
            "default_ddl_collation": params["default_ddl_collation"],
            "comment": data["comment"] or None,
        }

    def fetch_role_grant(self, fqn):
        try:
            show_result = execute(self.session, f"SHOW GRANTS OF ROLE {fqn.name}")
        except ProgrammingError:
            return None

        if len(show_result) == 0:
            return None

        grants = {"to_role": [], "to_user": []}

        for data in show_result:
            role_grant = {"role": data["role"], "owner": data["granted_by"]}
            if data["granted_to"] == "ROLE":
                role_grant["to_role"] = data["grantee_name"]
                grants["to_role"].append(role_grant)
            elif data["granted_to"] == "USER":
                role_grant["to_user"] = data["grantee_name"]
                grants["to_user"].append(role_grant)
            else:
                raise Exception(f"Unexpected role grant for role {fqn.name}", data)

        return grants

    def fetch_user(self, fqn):
        show_result = execute(self.session, f"SHOW USERS LIKE '{fqn.name}'", use_role="SECURITYADMIN")

        if len(show_result) == 0:
            return None
        if len(show_result) > 1:
            raise Exception(f"Found multiple users matching {fqn}")

        data = show_result[0]

        return {
            "name": data["name"],
            "login_name": data["login_name"],
            "display_name": data["display_name"],
            "first_name": data["first_name"] or None,
            "last_name": data["last_name"] or None,
            "email": data["email"] or None,
            "mins_to_unlock": data["mins_to_unlock"] or None,
            "days_to_expiry": data["days_to_expiry"] or None,
            "comment": data["comment"] or None,
            "disabled": data["disabled"] == "true",
            # "must_change_password": data["must_change_password"] == "true",
            "default_warehouse": data["default_warehouse"] or None,
            "default_namespace": data["default_namespace"] or None,
            "default_role": data["default_role"] or None,
            "default_secondary_roles": data["default_secondary_roles"] or None,
            "mins_to_bypass_mfa": data["mins_to_bypass_mfa"] or None,
            "owner": data["owner"],
        }

    def fetch_view(self, fqn):
        if fqn.schema is None:
            raise Exception(f"View fqn must have a schema {fqn}")
        try:
            show_result = execute(self.session, f"SHOW VIEWS LIKE '{fqn.name}' IN SCHEMA {fqn.database}.{fqn.schema}")
        except ProgrammingError:
            return None

        if len(show_result) == 0:
            return None
        if len(show_result) > 1:
            raise Exception(f"Found multiple views matching {fqn}")

        data = show_result[0]

        if data["is_materialized"] == "true":
            return None

        columns = self.fetch_columns("VIEW", fqn)

        return {
            "name": data["name"],
            "owner": data["owner"],
            "secure": data["is_secure"] == "true",
            "columns": columns,
            "change_tracking": data["change_tracking"] == "ON",
            "comment": data["comment"] or None,
            "as_": data["text"],
        }

    def fetch_warehouse(self, fqn):
        try:
            show_result = execute(self.session, f"SHOW WAREHOUSES LIKE '{fqn.name}'")
        except ProgrammingError:
            return None

        if len(show_result) == 0:
            return None
        if len(show_result) > 1:
            raise Exception(f"Found multiple warehouses matching {fqn}")

        data = show_result[0]

        show_params_result = execute(self.session, f"SHOW PARAMETERS FOR WAREHOUSE {fqn}")
        params = params_result_to_dict(show_params_result)

        query_accel = data.get("enable_query_acceleration")
        if query_accel:
            query_accel = query_accel == "true"

        return {
            "name": data["name"],
            "owner": data["owner"],
            "warehouse_type": data["type"],
            "warehouse_size": data["size"].upper(),
            # "max_cluster_count": data["max_cluster_count"],
            # "min_cluster_count": data["min_cluster_count"],
            # "scaling_policy": data["scaling_policy"],
            "auto_suspend": data["auto_suspend"],
            "auto_resume": data["auto_resume"] == "true",
            "comment": data["comment"] or None,
            "enable_query_acceleration": query_accel,
            "max_concurrency_level": params["max_concurrency_level"],
            "statement_queued_timeout_in_seconds": params["statement_queued_timeout_in_seconds"],
            "statement_timeout_in_seconds": params["statement_timeout_in_seconds"],
        }

    # endregion

    # region Resource Updates

    def update_resource(self, urn, data):
        try:
            return getattr(self, f"update_{urn.resource_key}")(urn.fqn, data)
        except AttributeError:
            raise NotImplementedError(f"Updates for resource {urn.resource_key} not supported")

    def update_user(self, fqn, data):
        attr, value = data.popitem()
        if attr == "owner":
            execute(self.session, f"GRANT OWNERSHIP ON USER {fqn} TO ROLE {value}")
        else:
            if isinstance(value, str):
                value = f"'{value}'"
            execute(self.session, f"ALTER USER {fqn} SET {attr} = {value}", use_role="USERADMIN")

    def update_warehouse(self, fqn, data):
        attr, value = data.popitem()
        if attr == "owner":
            execute(self.session, f"GRANT OWNERSHIP ON WAREHOUSE {fqn} TO ROLE {value}")
        else:
            execute(self.session, f"ALTER WAREHOUSE {fqn} SET {attr} = {value}")

    # endregion

    # region Resource Creation

    def create_resource(self, urn, data):
        if hasattr(self, f"create_{urn.resource_key}"):
            return getattr(self, f"create_{urn.resource_key}")(urn, data)

        use_role = data["owner"] if "owner" in data else None
        resource_cls = Resource.classes[urn.resource_key]
        resource = resource_cls(**data)
        sql = resource.create_sql()
        execute(self.session, sql, use_role=use_role)

    def create_role(self, urn, data):
        role = Role(**data)
        sql = role.create_sql()
        execute(self.session, sql, use_role="USERADMIN")
        execute(self.session, f"GRANT OWNERSHIP ON ROLE {role.name} TO ROLE {role.owner}", use_role="USERADMIN")

    def create_account_grant(self, urn, data):
        grant = AccountGrant(**data)
        sql = grant.create_sql()
        res = execute(self.session, sql, use_role="SYSADMIN")
        _fail_if_not_granted(res)

    def create_role_grant(self, urn, container):
        grants = container["to_role"] + container["to_user"]
        for data in grants:
            grant = RoleGrant(**data)
            sql = grant.create_sql()
            # According to the docs, SECURITYADMIN should issue the GRANT ROLE command but
            # the grant itself will be owned by SYSADMIN (or maybe the role owner?), even if SECURITYADMIN runs the command
            res = execute(self.session, sql, use_role="SECURITYADMIN")
            _fail_if_not_granted(res)

    # endregion

    # region Resource Destruction

    def drop_resource(self, urn, data):
        if hasattr(self, f"drop_{urn.resource_key}"):
            return getattr(self, f"drop_{urn.resource_key}")(urn, data)

        use_role = data["owner"] if "owner" in data else None
        resource_cls = Resource.classes[urn.resource_key]
        resource = resource_cls(**data)
        sql = resource.drop_sql()
        execute(self.session, sql, use_role=use_role)

    def drop_role_grant(self, urn, container):
        grants = container["to_role"] + container["to_user"]
        for data in grants:
            grant = RoleGrant(**data)
            sql = grant.drop_sql()
            execute(self.session, sql, use_role="SYSADMIN")

    # endregion
