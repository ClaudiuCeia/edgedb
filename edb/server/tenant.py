#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2023-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

from __future__ import annotations
from typing import *

import asyncio
import contextlib
import functools
import json
import logging
import pathlib
import pickle
import struct
import sys
import time

import immutables

from edb import errors
from edb.common import retryloop
from edb.common import taskgroup

from . import args as srvargs
from . import config
from . import connpool
from . import dbview
from . import defines
from . import metrics
from . import pgcon
from .ha import adaptive as adaptive_ha
from .ha import base as ha_base
from .pgcon import errors as pgcon_errors

if TYPE_CHECKING:
    from edb.pgsql import params as pgparams

    from . import pgcluster
    from . import server as edbserver


logger = logging.getLogger("edb.server")


class RoleDescriptor(TypedDict):
    superuser: bool
    name: str
    password: str | None


class Tenant(ha_base.ClusterProtocol):
    _server: edbserver.BaseServer
    _cluster: pgcluster.BaseCluster
    _tenant_id: str
    _instance_name: str
    _instance_data: Mapping[str, str]
    _dbindex: dbview.DatabaseIndex | None
    _initing: bool
    _running: bool
    _accepting_connections: bool

    __loop: asyncio.AbstractEventLoop
    _task_group: taskgroup.TaskGroup | None
    _tasks: Set[asyncio.Task]
    _accept_new_tasks: bool

    __sys_pgcon: pgcon.PGConnection | None
    _sys_pgcon_waiter: asyncio.Lock
    _sys_pgcon_ready_evt: asyncio.Event
    _sys_pgcon_reconnect_evt: asyncio.Event
    _max_backend_connections: int
    _suggested_client_pool_size: int
    _pg_pool: connpool.Pool
    _pg_unavailable_msg: str | None

    _ha_master_serial: int
    _backend_adaptive_ha: adaptive_ha.AdaptiveHASupport | None
    _readiness_state_file: str | None
    _readiness: srvargs.ReadinessState
    _readiness_reason: str

    # A set of databases that should not accept new connections.
    _block_new_connections: set[str]
    _report_config_data: dict[defines.ProtocolVersion, bytes]

    _roles: Mapping[str, RoleDescriptor]
    _sys_auth: Tuple[Any, ...]
    _jwt_sub_allowlist_file: pathlib.Path | None
    _jwt_sub_allowlist: frozenset[str] | None
    _jwt_revocation_list_file: pathlib.Path | None
    _jwt_revocation_list: frozenset[str] | None

    def __init__(
        self,
        cluster: pgcluster.BaseCluster,
        *,
        instance_name: str,
        max_backend_connections: int,
        backend_adaptive_ha: bool = False,
        readiness_state_file: str | None = None,
        jwt_sub_allowlist_file: pathlib.Path | None = None,
        jwt_revocation_list_file: pathlib.Path | None = None,
    ):
        self._cluster = cluster
        self._tenant_id = self.get_backend_runtime_params().tenant_id
        self._instance_name = instance_name
        self._instance_data = immutables.Map()
        self._initing = True
        self._running = False
        self._accepting_connections = False

        self._task_group = None
        self._tasks = set()
        self._accept_new_tasks = False

        # Never use `self.__sys_pgcon` directly; get it via
        # `async with self.use_sys_pgcon()`.
        self.__sys_pgcon = None

        # Increase-only counter to reject outdated attempts to connect
        self._ha_master_serial = 0
        if backend_adaptive_ha:
            self._backend_adaptive_ha = adaptive_ha.AdaptiveHASupport(
                self, self._instance_name
            )
        else:
            self._backend_adaptive_ha = None
        self._readiness_state_file = readiness_state_file
        self._readiness = srvargs.ReadinessState.Default
        self._readiness_reason = ""

        self._max_backend_connections = max_backend_connections
        self._suggested_client_pool_size = max(
            min(
                max_backend_connections, defines.MAX_SUGGESTED_CLIENT_POOL_SIZE
            ),
            defines.MIN_SUGGESTED_CLIENT_POOL_SIZE,
        )
        self._pg_pool = connpool.Pool(
            connect=self._pg_connect,
            disconnect=self._pg_disconnect,
            # 1 connection is reserved for the system DB
            max_capacity=max_backend_connections - 1,
        )
        self._pg_unavailable_msg = None
        self._block_new_connections = set()
        self._report_config_data = {}

        # DB state will be initialized in init().
        self._dbindex = None

        self._roles = immutables.Map()
        self._sys_auth = tuple()
        self._jwt_sub_allowlist_file = jwt_sub_allowlist_file
        self._jwt_sub_allowlist = None
        self._jwt_revocation_list_file = jwt_revocation_list_file
        self._jwt_revocation_list = None

    def set_server(self, server: edbserver.BaseServer) -> None:
        self._server = server
        self.__loop = server.get_loop()

    def on_switch_over(self):
        # Bumping this serial counter will "cancel" all pending connections
        # to the old master.
        self._ha_master_serial += 1

        if self._accept_new_tasks:
            self.create_task(
                self._pg_pool.prune_all_connections(),
                interruptable=True,
            )

        if self.__sys_pgcon is None:
            # Assume a reconnect task is already running, now that we know the
            # new master is likely ready, let's just give the task a push.
            self._sys_pgcon_reconnect_evt.set()
        else:
            # Brutally close the sys_pgcon to the old master - this should
            # trigger a reconnect task.
            self.__sys_pgcon.abort()

        if self._backend_adaptive_ha is not None:
            # Switch to FAILOVER if adaptive HA is enabled
            self._backend_adaptive_ha.set_state_failover(
                call_on_switch_over=False
            )

    def get_active_pgcon_num(self) -> int:
        return (
            self._pg_pool.current_capacity - self._pg_pool.get_pending_conns()
        )

    @property
    def client_id(self) -> int:
        return self._cluster.get_client_id()

    @property
    def server(self) -> edbserver.BaseServer:
        return self._server

    @property
    def tenant_id(self) -> str:
        return self._tenant_id

    @property
    def suggested_client_pool_size(self) -> int:
        return self._suggested_client_pool_size

    def get_pg_dbname(self, dbname: str) -> str:
        return self._cluster.get_db_name(dbname)

    def get_pgaddr(self) -> Dict[str, Any]:
        return self._cluster.get_connection_spec()

    @functools.lru_cache
    def get_backend_runtime_params(self) -> pgparams.BackendRuntimeParams:
        return self._cluster.get_runtime_params()

    def get_instance_name(self) -> str:
        return self._instance_name

    def get_instance_data(self, key: str) -> str:
        return self._instance_data[key]

    def is_online(self) -> bool:
        return self._readiness is not srvargs.ReadinessState.Offline

    def is_blocked(self) -> bool:
        return self._readiness is srvargs.ReadinessState.Blocked

    def is_ready(self) -> bool:
        return (
            self._readiness is srvargs.ReadinessState.Default
            or self._readiness is srvargs.ReadinessState.ReadOnly
        )

    def is_readonly(self) -> bool:
        return self._readiness is srvargs.ReadinessState.ReadOnly

    def get_readiness_reason(self) -> str:
        return self._readiness_reason

    def get_sys_config(self) -> Mapping[str, config.SettingValue]:
        assert self._dbindex is not None
        return self._dbindex.get_sys_config()

    def get_report_config_data(
        self,
        protocol_version: defines.ProtocolVersion,
    ) -> bytes:
        if protocol_version >= (2, 0):
            return self._report_config_data[(2, 0)]
        else:
            return self._report_config_data[(1, 0)]

    def get_global_schema_pickle(self) -> bytes:
        assert self._dbindex is not None
        return self._dbindex.get_global_schema_pickle()

    def get_db(self, *, dbname: str) -> dbview.Database:
        assert self._dbindex is not None
        return self._dbindex.get_db(dbname)

    def maybe_get_db(self, *, dbname: str) -> dbview.Database:
        assert self._dbindex is not None
        return self._dbindex.maybe_get_db(dbname)

    def is_accepting_connections(self) -> bool:
        return self._accepting_connections and self._accept_new_tasks

    def get_roles(self) -> Mapping[str, RoleDescriptor]:
        return self._roles

    def set_roles(self, roles: Mapping[str, RoleDescriptor]) -> None:
        self._roles = roles

    async def _fetch_roles(self, syscon: pgcon.PGConnection) -> None:
        role_query = self._server.get_sys_query("roles")
        json_data = await syscon.sql_fetch_val(role_query, use_prep_stmt=True)
        roles = json.loads(json_data)
        self._roles = immutables.Map([(r["name"], r) for r in roles])

    async def init_sys_pgcon(self) -> None:
        self._sys_pgcon_waiter = asyncio.Lock()
        self.__sys_pgcon = await self._pg_connect(defines.EDGEDB_SYSTEM_DB)
        self._sys_pgcon_ready_evt = asyncio.Event()
        self._sys_pgcon_reconnect_evt = asyncio.Event()

    async def init(self) -> None:
        logger.debug("starting database introspection")
        async with self.use_sys_pgcon() as syscon:
            result = await syscon.sql_fetch_val(
                b"""\
                    SELECT json::json FROM edgedbinstdata.instdata
                    WHERE key = 'instancedata';
                """
            )
            self._instance_data = immutables.Map(json.loads(result))
            await self._fetch_roles(syscon)
            if self._server.get_compiler_pool() is None:
                # Parse global schema in I/O process if this is done only once
                logger.debug("parsing global schema locally")
                global_schema_pickle = pickle.dumps(
                    await self._server.introspect_global_schema(syscon), -1
                )
                data = None
            else:
                # Multi-tenant server defers the parsing into the compiler
                data = await self._server.introspect_global_schema_json(syscon)
                compiler_pool = self._server.get_compiler_pool()

        if data is not None:
            logger.debug("parsing global schema")
            global_schema_pickle = (
                await compiler_pool.parse_global_schema(data)
            )

        logger.info("loading system config")
        sys_config = await self._load_sys_config()
        default_sysconfig = await self._load_sys_config("sysconfig_default")
        await self._load_reported_config()

        self._dbindex = dbview.DatabaseIndex(
            self,
            std_schema=self._server.get_std_schema(),
            global_schema_pickle=global_schema_pickle,
            sys_config=sys_config,
            default_sysconfig=default_sysconfig,
            sys_config_spec=self._server.config_settings,
        )

        await self._introspect_dbs()

        # Now, once all DBs have been introspected, start listening on
        # any notifications about schema/roles/etc changes.
        assert self.__sys_pgcon is not None
        await self.__sys_pgcon.listen_for_sysevent()
        self.__sys_pgcon.mark_as_system_db()
        self._sys_pgcon_ready_evt.set()

        self.populate_sys_auth()

        if self._readiness_state_file is not None:

            def reload_state_file(_file_modified, _event):
                self.reload_readiness_state()

            self.reload_readiness_state()
            self._server.monitor_fs(
                self._readiness_state_file, reload_state_file
            )

        self._initing = False

    async def start_accepting_new_tasks(self) -> None:
        assert self._task_group is None
        self._task_group = taskgroup.TaskGroup()
        await self._task_group.__aenter__()
        self._accept_new_tasks = True
        await self._cluster.start_watching(self.on_switch_over)

    def start_running(self) -> None:
        self._running = True
        self._accepting_connections = True

    def stop_accepting_connections(self) -> None:
        self._accepting_connections = False

    @property
    def accept_new_tasks(self):
        return self._accept_new_tasks

    def create_task(
        self, coro: Coroutine, *, interruptable: bool
    ) -> asyncio.Task:
        # Interruptable tasks are regular asyncio tasks that may be interrupted
        # randomly in the middle when the event loop stops; while tasks with
        # interruptable=False are always awaited before the server stops, so
        # that e.g. all finally blocks get a chance to execute in those tasks.
        # Therefore, it is an error trying to create a task while the server is
        # not expecting one, so always couple the call with an additional check
        if self._accept_new_tasks and self._task_group is not None:
            if interruptable:
                rv = self.__loop.create_task(coro)
            else:
                rv = self._task_group.create_task(coro)

            # Keep a strong reference of the created Task
            self._tasks.add(rv)
            rv.add_done_callback(self._tasks.discard)

            return rv
        else:
            # Hint: add `if tenant.accept_new_tasks` before `.create_task()`
            raise RuntimeError("task cannot be created at this time")

    def stop(self) -> None:
        self._running = False
        self._accept_new_tasks = False
        self._cluster.stop_watching()

    async def wait_stopped(self) -> None:
        if self._task_group is not None:
            tg = self._task_group
            self._task_group = None
            await tg.__aexit__(*sys.exc_info())

    def terminate_sys_pgcon(self) -> None:
        if self.__sys_pgcon is not None:
            self.__sys_pgcon.terminate()
            self.__sys_pgcon = None
        del self._sys_pgcon_waiter

    async def _pg_connect(self, dbname: str) -> pgcon.PGConnection:
        ha_serial = self._ha_master_serial
        if self.get_backend_runtime_params().has_create_database:
            pg_dbname = self.get_pg_dbname(dbname)
        else:
            pg_dbname = self.get_pg_dbname(defines.EDGEDB_SUPERUSER_DB)
        started_at = time.monotonic()
        try:
            rv = await pgcon.connect(
                self.get_pgaddr(), pg_dbname, self.get_backend_runtime_params()
            )
            if self._server.stmt_cache_size is not None:
                rv.set_stmt_cache_size(self._server.stmt_cache_size)
        except Exception:
            metrics.backend_connection_establishment_errors.inc(
                1.0, self._instance_name
            )
            raise
        finally:
            metrics.backend_connection_establishment_latency.observe(
                time.monotonic() - started_at, self._instance_name
            )
        if ha_serial == self._ha_master_serial:
            rv.set_tenant(self)
            if self._backend_adaptive_ha is not None:
                self._backend_adaptive_ha.on_pgcon_made(
                    dbname == defines.EDGEDB_SYSTEM_DB
                )
            metrics.total_backend_connections.inc(1.0, self._instance_name)
            metrics.current_backend_connections.inc(1.0, self._instance_name)
            return rv
        else:
            rv.terminate()
            raise ConnectionError("connected to outdated Postgres master")

    async def _pg_disconnect(self, conn: pgcon.PGConnection) -> None:
        metrics.current_backend_connections.dec(1.0, self._instance_name)
        conn.terminate()

    @contextlib.asynccontextmanager
    async def direct_pgcon(
        self,
        dbname: str,
    ) -> AsyncGenerator[pgcon.PGConnection, None]:
        conn = None
        try:
            conn = await self._pg_connect(dbname)
            yield conn
        finally:
            if conn is not None:
                await self._pg_disconnect(conn)

    @contextlib.asynccontextmanager
    async def use_sys_pgcon(self) -> AsyncGenerator[pgcon.PGConnection, None]:
        if not self._initing and not self._running:
            raise RuntimeError("EdgeDB server is not running.")

        await self._sys_pgcon_waiter.acquire()

        if not self._initing and not self._running:
            self._sys_pgcon_waiter.release()
            raise RuntimeError("EdgeDB server is not running.")

        if self.__sys_pgcon is None or not self.__sys_pgcon.is_healthy():
            conn, self.__sys_pgcon = self.__sys_pgcon, None
            if conn is not None:
                self._sys_pgcon_ready_evt.clear()
                conn.abort()
            # We depend on the reconnect on connection_lost() of __sys_pgcon
            await self._sys_pgcon_ready_evt.wait()
            if self.__sys_pgcon is None:
                self._sys_pgcon_waiter.release()
                raise RuntimeError("Cannot acquire pgcon to the system DB.")

        try:
            yield self.__sys_pgcon
        finally:
            self._sys_pgcon_waiter.release()

    def set_stmt_cache_size(self, size: int) -> None:
        for conn in self._pg_pool.iterate_connections():
            conn.set_stmt_cache_size(size)

    def on_sys_pgcon_parameter_status_updated(
        self,
        name: str,
        value: str,
    ) -> None:
        try:
            if name == "in_hot_standby" and value == "on":
                # It is a strong evidence of failover if the sys_pgcon receives
                # a notification that in_hot_standby is turned on.
                self.on_sys_pgcon_failover_signal()
        except Exception:
            metrics.background_errors.inc(
                1.0,
                self._instance_name,
                "on_sys_pgcon_parameter_status_updated"
            )
            raise

    def on_sys_pgcon_failover_signal(self) -> None:
        if not self._running:
            return
        try:
            if self._backend_adaptive_ha is not None:
                # Switch to FAILOVER if adaptive HA is enabled
                self._backend_adaptive_ha.set_state_failover()
            elif getattr(self._cluster, "_ha_backend", None) is None:
                # If the server is not using an HA backend, nor has enabled the
                # adaptive HA monitoring, we still try to "switch over" by
                # disconnecting all pgcons if failover signal is received,
                # allowing reconnection to happen sooner.
                self.on_switch_over()
            # Else, the HA backend should take care of calling on_switch_over()
        except Exception:
            metrics.background_errors.inc(
                1.0, self._instance_name, "on_sys_pgcon_failover_signal"
            )
            raise

    def on_sys_pgcon_connection_lost(self, exc: Exception | None) -> None:
        try:
            if not self._running:
                # The tenant is shutting down, release all events so that
                # the waiters if any could continue and exit
                self._sys_pgcon_ready_evt.set()
                self._sys_pgcon_reconnect_evt.set()
                return

            logger.error(
                "Connection to the system database is "
                + ("closed." if exc is None else f"broken! Reason: {exc}")
            )
            self.set_pg_unavailable_msg(
                "Connection is lost, please check server log for the reason."
            )
            self.__sys_pgcon = None
            self._sys_pgcon_ready_evt.clear()
            if self._accept_new_tasks:
                self.create_task(
                    self._reconnect_sys_pgcon(), interruptable=True
                )
            self.on_pgcon_broken(True)
        except Exception:
            metrics.background_errors.inc(
                1.0, self._instance_name, "on_sys_pgcon_connection_lost"
            )
            raise

    async def _reconnect_sys_pgcon(self) -> None:
        try:
            conn = None
            while self._running:
                try:
                    conn = await self._pg_connect(defines.EDGEDB_SYSTEM_DB)
                    break
                except OSError:
                    # Keep retrying as far as:
                    #   1. This tenant is still running,
                    #   2. We still cannot connect to the Postgres cluster, or
                    pass
                except pgcon_errors.BackendError as e:
                    #   3. The Postgres cluster is still starting up, or the
                    #      HA failover is still in progress
                    if not (
                        e.code_is(pgcon_errors.ERROR_FEATURE_NOT_SUPPORTED)
                        or e.code_is(pgcon_errors.ERROR_CANNOT_CONNECT_NOW)
                        or e.code_is(
                            pgcon_errors.ERROR_READ_ONLY_SQL_TRANSACTION
                        )
                    ):
                        # TODO: ERROR_FEATURE_NOT_SUPPORTED should be removed
                        # once PostgreSQL supports SERIALIZABLE in hot standbys
                        raise

                if self._running:
                    try:
                        # Retry after INTERVAL seconds, unless the event is set
                        # and we can retry immediately after the event.
                        await asyncio.wait_for(
                            self._sys_pgcon_reconnect_evt.wait(),
                            defines.SYSTEM_DB_RECONNECT_INTERVAL,
                        )
                        # But the event can only skip one INTERVAL.
                        self._sys_pgcon_reconnect_evt.clear()
                    except asyncio.TimeoutError:
                        pass

            if not self._running:
                if conn is not None:
                    conn.abort()
                return

            assert conn is not None
            logger.info("Successfully reconnected to the system database.")
            self.__sys_pgcon = conn
            self.__sys_pgcon.mark_as_system_db()
            # This await is meant to be after mark_as_system_db() because we
            # need the pgcon to be able to trigger another reconnect if its
            # connection is lost during this await.
            await self.__sys_pgcon.listen_for_sysevent()
            self.set_pg_unavailable_msg(None)
        finally:
            self._sys_pgcon_ready_evt.set()

    def on_pgcon_broken(self, is_sys_pgcon: bool = False) -> None:
        try:
            if self._backend_adaptive_ha:
                self._backend_adaptive_ha.on_pgcon_broken(is_sys_pgcon)
        except Exception:
            metrics.background_errors.inc(
                1.0, self._instance_name, "on_pgcon_broken"
            )
            raise

    def on_pgcon_lost(self) -> None:
        try:
            if self._backend_adaptive_ha:
                self._backend_adaptive_ha.on_pgcon_lost()
        except Exception:
            metrics.background_errors.inc(
                1.0, self._instance_name, "on_pgcon_lost")
            raise

    def set_pg_unavailable_msg(self, msg: str | None) -> None:
        if msg is None or self._pg_unavailable_msg is None:
            self._pg_unavailable_msg = msg

    async def acquire_pgcon(self, dbname: str) -> pgcon.PGConnection:
        if self._pg_unavailable_msg is not None:
            raise errors.BackendUnavailableError(
                "Postgres is not available: " + self._pg_unavailable_msg
            )

        for _ in range(self._pg_pool.max_capacity):
            conn = await self._pg_pool.acquire(dbname)
            if conn.is_healthy():
                return conn
            else:
                logger.warning("Acquired an unhealthy pgcon; discard now.")
                self._pg_pool.release(dbname, conn, discard=True)
        else:
            # This is unlikely to happen, but we defer to the caller to retry
            # when it does happen
            raise errors.BackendUnavailableError(
                "No healthy backend connection available at the moment, "
                "please try again."
            )

    def release_pgcon(
        self,
        dbname: str,
        conn: pgcon.PGConnection,
        *,
        discard: bool = False,
    ) -> None:
        if not conn.is_healthy():
            if not discard:
                logger.warning("Released an unhealthy pgcon; discard now.")
            discard = True
        try:
            self._pg_pool.release(dbname, conn, discard=discard)
        except Exception:
            metrics.background_errors.inc(
                1.0, self._instance_name, "release_pgcon"
            )
            raise

    def allow_database_connections(self, dbname: str) -> None:
        self._block_new_connections.discard(dbname)

    def is_database_connectable(self, dbname: str) -> bool:
        return (
            dbname != defines.EDGEDB_TEMPLATE_DB
            and dbname not in self._block_new_connections
        )

    async def ensure_database_not_connected(self, dbname: str) -> None:
        if self._dbindex and self._dbindex.count_connections(dbname):
            # If there are open EdgeDB connections to the `dbname` DB
            # just raise the error Postgres would have raised itself.
            raise errors.ExecutionError(
                f"database {dbname!r} is being accessed by other users"
            )
        else:
            self._block_new_connections.add(dbname)

            # Prune our inactive connections.
            await self._pg_pool.prune_inactive_connections(dbname)

            # Signal adjacent servers to prune their connections to this
            # database.
            await self.signal_sysevent(
                "ensure-database-not-used", dbname=dbname
            )

            rloop = retryloop.RetryLoop(
                backoff=retryloop.exp_backoff(),
                timeout=10.0,
                ignore=errors.ExecutionError,
            )

            async for iteration in rloop:
                async with iteration:
                    await self._pg_ensure_database_not_connected(dbname)

    async def _pg_ensure_database_not_connected(self, dbname: str) -> None:
        async with self.use_sys_pgcon() as pgcon:
            conns = await pgcon.sql_fetch_col(
                b"""
                SELECT
                    pid
                FROM
                    pg_stat_activity
                WHERE
                    datname = $1
                """,
                args=[dbname.encode("utf-8")],
            )

        if conns:
            raise errors.ExecutionError(
                f"database {dbname!r} is being accessed by other users"
            )

    async def _acquire_intro_pgcon(
        self, dbname: str
    ) -> pgcon.PGConnection | None:
        try:
            conn = await self.acquire_pgcon(dbname)
        except pgcon_errors.BackendError as e:
            if e.code_is(pgcon_errors.ERROR_INVALID_CATALOG_NAME):
                # database does not exist (anymore)
                logger.warning(
                    "Detected concurrently-dropped database %s; skipping.",
                    dbname,
                )
                if self._dbindex is not None and self._dbindex.has_db(dbname):
                    self._dbindex.unregister_db(dbname)
                return None
            else:
                raise
        return conn

    async def _introspect_extensions(
        self, conn: pgcon.PGConnection
    ) -> set[str]:
        extension_names_json = await conn.sql_fetch_val(
            b"""
                SELECT json_agg(name) FROM edgedb."_SchemaExtension";
            """,
        )
        if extension_names_json:
            extensions = set(json.loads(extension_names_json))
        else:
            extensions = set()

        return extensions

    async def introspect_db(self, dbname: str) -> None:
        """Use this method to (re-)introspect a DB.

        If the DB is already registered in self._dbindex, its
        schema, config, etc. would simply be updated. If it's missing
        an entry for it would be created.

        All remote notifications of remote events should use this method
        to refresh the state. Even if the remote event was a simple config
        change, a lot of other events could happen before it was sent to us
        by a remote server and us receiving it. E.g. a DB could have been
        dropped and recreated again. It's safer to refresh the entire state
        than refreshing individual components of it. Besides, DDL and
        database-level config modifications are supposed to be rare events.
        """
        logger.info("introspecting database '%s'", dbname)

        conn = await self._acquire_intro_pgcon(dbname)
        if not conn:
            return

        try:
            user_schema_json = (
                await self._server.introspect_user_schema_json(conn)
            )

            reflection_cache_json = await conn.sql_fetch_val(
                b"""
                    SELECT json_agg(o.c)
                    FROM (
                        SELECT
                            json_build_object(
                                'eql_hash', t.eql_hash,
                                'argnames', array_to_json(t.argnames)
                            ) AS c
                        FROM
                            ROWS FROM(edgedb._get_cached_reflection())
                                AS t(eql_hash text, argnames text[])
                    ) AS o;
                """,
            )

            reflection_cache = immutables.Map(
                {
                    r["eql_hash"]: tuple(r["argnames"])
                    for r in json.loads(reflection_cache_json)
                }
            )

            backend_ids_json = await conn.sql_fetch_val(
                b"""
                SELECT
                    json_object_agg(
                        "id"::text,
                        "backend_id"
                    )::text
                FROM
                    edgedb."_SchemaType"
                """,
            )
            backend_ids = json.loads(backend_ids_json)

            db_config_json = await self._server.introspect_db_config(conn)

            extensions = await self._introspect_extensions(conn)
        finally:
            self.release_pgcon(dbname, conn)

        compiler_pool = self._server.get_compiler_pool()
        parsed_db = await compiler_pool.parse_user_schema_db_config(
            user_schema_json, db_config_json, self.get_global_schema_pickle()
        )
        assert self._dbindex is not None
        db = self._dbindex.register_db(
            dbname,
            user_schema_pickle=parsed_db.user_schema_pickle,
            db_config=parsed_db.database_config,
            reflection_cache=reflection_cache,
            backend_ids=backend_ids,
            extensions=extensions,
            ext_config_settings=parsed_db.ext_config_settings,
        )
        db.set_state_serializer(
            parsed_db.protocol_version,
            parsed_db.state_serializer,
        )

    async def _early_introspect_db(self, dbname: str) -> None:
        """We need to always introspect the extensions for each database.

        Otherwise, we won't know to accept connections for graphql or
        http, for example, until a native connection is made.
        """
        logger.info("introspecting extensions for database '%s'", dbname)

        conn = await self._acquire_intro_pgcon(dbname)
        if not conn:
            return

        try:
            assert self._dbindex is not None
            if not self._dbindex.has_db(dbname):
                extensions = await self._introspect_extensions(conn)
                # Re-check in case we have a concurrent introspection task.
                if not self._dbindex.has_db(dbname):
                    self._dbindex.register_db(
                        dbname,
                        user_schema_pickle=None,
                        db_config=None,
                        reflection_cache=None,
                        backend_ids=None,
                        extensions=extensions,
                        ext_config_settings=None,
                    )
        finally:
            self.release_pgcon(dbname, conn)

    async def _introspect_dbs(self) -> None:
        async with self.use_sys_pgcon() as syscon:
            dbnames = await self._server.get_dbnames(syscon)

        async with taskgroup.TaskGroup(name="introspect DB extensions") as g:
            for dbname in dbnames:
                # There's a risk of the DB being dropped by another server
                # between us building the list of databases and loading
                # information about them.
                g.create_task(self._early_introspect_db(dbname))

    async def _load_reported_config(self) -> None:
        async with self.use_sys_pgcon() as syscon:
            try:
                data = await syscon.sql_fetch_val(
                    self._server.get_sys_query("report_configs"),
                    use_prep_stmt=True,
                )

                for (
                    protocol_ver,
                    typedesc,
                ) in self._server.get_report_config_typedesc().items():
                    self._report_config_data[protocol_ver] = (
                        struct.pack("!L", len(typedesc))
                        + typedesc
                        + struct.pack("!L", len(data))
                        + data
                    )
            except Exception:
                metrics.background_errors.inc(
                    1.0, self._instance_name, "load_reported_config"
                )
                raise

    async def _load_sys_config(
        self,
        query_name: str = "sysconfig",
    ) -> Mapping[str, config.SettingValue]:
        async with self.use_sys_pgcon() as syscon:
            query = self._server.get_sys_query(query_name)
            sys_config_json = await syscon.sql_fetch_val(query)

        return config.from_json(self._server.config_settings, sys_config_json)

    async def _reintrospect_global_schema(self) -> None:
        if not self._initing and not self._running:
            logger.warning(
                "global-schema-changes event received during shutdown; "
                "ignoring."
            )
            return
        async with self.use_sys_pgcon() as syscon:
            data = await self._server.introspect_global_schema_json(syscon)
            await self._fetch_roles(syscon)
        compiler_pool = self._server.get_compiler_pool()
        global_schema_pickle = await compiler_pool.parse_global_schema(data)
        assert self._dbindex is not None
        self._dbindex.update_global_schema(global_schema_pickle)

    def populate_sys_auth(self) -> None:
        assert self._dbindex is not None
        cfg = self._dbindex.get_sys_config()
        auth = self._server.config_lookup("auth", cfg) or ()
        self._sys_auth = tuple(sorted(auth, key=lambda a: a.priority))

    async def get_auth_method(
        self,
        user: str,
        transport: srvargs.ServerConnTransport,
    ) -> Any:
        authlist = self._sys_auth

        if authlist:
            for auth in authlist:
                match = (user in auth.user or "*" in auth.user) and (
                    not auth.method.transports
                    or transport in auth.method.transports
                )

                if match:
                    return auth.method

        default_method = self._server.get_default_auth_method(transport)
        auth_type = self._server.config_settings.get_type_by_name(
            f'cfg::{default_method.value}'
        )
        return auth_type()

    async def new_dbview(
        self,
        *,
        dbname: str,
        query_cache: bool,
        protocol_version: defines.ProtocolVersion,
    ) -> dbview.DatabaseConnectionView:
        db = self.get_db(dbname=dbname)
        await db.introspection()
        assert self._dbindex is not None
        return self._dbindex.new_view(
            dbname, query_cache=query_cache, protocol_version=protocol_version
        )

    def remove_dbview(self, dbview_: dbview.DatabaseConnectionView) -> None:
        assert self._dbindex is not None
        return self._dbindex.remove_view(dbview_)

    def schedule_reported_config_if_needed(self, setting_name: str) -> None:
        setting = self._server.config_settings.get(setting_name)
        if setting and setting.report and self._accept_new_tasks:
            self.create_task(self._load_reported_config(), interruptable=True)

    def load_jwcrypto(self) -> None:
        if self._jwt_sub_allowlist_file is not None:
            logger.info(
                "(re-)loading JWT subject allowlist from "
                f'"{self._jwt_sub_allowlist_file}"'
            )
            try:
                self._jwt_sub_allowlist = frozenset(
                    self._jwt_sub_allowlist_file.read_text().splitlines(),
                )
            except Exception as e:
                raise edbserver.StartupError(
                    f"cannot load JWT sub allowlist: {e}"
                ) from e

        if self._jwt_revocation_list_file is not None:
            logger.info(
                "(re-)loading JWT revocation list from "
                f'"{self._jwt_revocation_list_file}"'
            )
            try:
                self._jwt_revocation_list = frozenset(
                    self._jwt_revocation_list_file.read_text().splitlines(),
                )
            except Exception as e:
                raise edbserver.StartupError(
                    f"cannot load JWT revocation list: {e}"
                ) from e

    def check_jwt(self, claims: dict[str, Any]) -> None:
        """Check JWT for validity"""

        if self._jwt_sub_allowlist is not None:
            subject = claims.get("sub")
            if not subject:
                raise errors.AuthenticationError(
                    "authentication failed: "
                    "JWT does not contain a valid subject claim"
                )
            if subject not in self._jwt_sub_allowlist:
                raise errors.AuthenticationError(
                    "authentication failed: unauthorized subject"
                )

        if self._jwt_revocation_list is not None:
            key_id = claims.get("jti")
            if not key_id:
                raise errors.AuthenticationError(
                    "authentication failed: "
                    "JWT does not contain a valid key id"
                )
            if key_id in self._jwt_revocation_list:
                raise errors.AuthenticationError(
                    "authentication failed: revoked key"
                )

    def reload_readiness_state(self) -> None:
        if self._readiness_state_file is None:
            return
        try:
            with open(self._readiness_state_file, "rt") as rt:
                line = rt.readline().strip()
                try:
                    state, _, reason = line.partition(":")
                    self._readiness = srvargs.ReadinessState(state)
                    self._readiness_reason = reason
                    logger.info(
                        "readiness state file changed, "
                        "setting server readiness to %r%s",
                        state,
                        f" ({reason})" if reason else "",
                    )
                except ValueError:
                    logger.warning(
                        "invalid state in readiness state file (%r): %r, "
                        "resetting server readiness to 'default'",
                        self._readiness_state_file,
                        state,
                    )
                    self._readiness = srvargs.ReadinessState.Default

        except FileNotFoundError:
            logger.info(
                "readiness state file (%s) removed, resetting "
                "server readiness to 'default'",
                self._readiness_state_file,
            )
            self._readiness = srvargs.ReadinessState.Default

        except Exception as e:
            logger.warning(
                "cannot read readiness state file (%s): %s, "
                "resetting server readiness to 'default'",
                self._readiness_state_file,
                e,
            )
            self._readiness = srvargs.ReadinessState.Default

        self._accepting_connections = self.is_online()

    async def on_before_drop_db(
        self, dbname: str, current_dbname: str
    ) -> None:
        if current_dbname == dbname:
            raise errors.ExecutionError(
                f"cannot drop the currently open database {dbname!r}"
            )

        await self.ensure_database_not_connected(dbname)

    async def on_before_create_db_from_template(
        self, dbname: str, current_dbname: str
    ) -> None:
        if current_dbname == dbname:
            raise errors.ExecutionError(
                f"cannot create database using currently open database "
                f"{dbname!r} as a template database"
            )

        await self.ensure_database_not_connected(dbname)

    def on_after_drop_db(self, dbname: str) -> None:
        try:
            assert self._dbindex is not None
            if self._dbindex.has_db(dbname):
                self._dbindex.unregister_db(dbname)
            self._block_new_connections.discard(dbname)
        except Exception:
            metrics.background_errors.inc(
                1.0, self._instance_name, "on_after_drop_db"
            )
            raise

    async def cancel_pgcon_operation(self, con: pgcon.PGConnection) -> bool:
        async with self.use_sys_pgcon() as syscon:
            if con.idle:
                # con could have received the query results while we
                # were acquiring a system connection to cancel it.
                return False

            if con.is_cancelling():
                # Somehow the connection is already being cancelled and
                # we don't want to have to cancellations go in parallel.
                return False

            con.start_pg_cancellation()
            try:
                # Returns True if the `pid` exists and it was able to send it a
                # SIGINT.  Will throw an exception if the privileges aren't
                # sufficient.
                result = await syscon.sql_fetch_val(
                    f"SELECT pg_cancel_backend({con.backend_pid});".encode(),
                )
            finally:
                con.finish_pg_cancellation()

            return result == b"\x01"

    async def cancel_and_discard_pgcon(
        self,
        con: pgcon.PGConnection,
        dbname: str,
    ) -> None:
        try:
            if self._running:
                await self.cancel_pgcon_operation(con)
        finally:
            self.release_pgcon(dbname, con, discard=True)

    async def signal_sysevent(self, event: str, **kwargs) -> None:
        try:
            if not self._initing and not self._running:
                # This is very likely if we are doing
                # "run_startup_script_and_exit()", but is also possible if the
                # tenant was shut down with this coroutine as a background task
                # in flight.
                return

            async with self.use_sys_pgcon() as con:
                await con.signal_sysevent(event, **kwargs)
        except Exception:
            metrics.background_errors.inc(
                1.0, self._instance_name, "signal_sysevent"
            )
            raise

    def on_remote_database_quarantine(self, dbname: str) -> None:
        if not self._accept_new_tasks:
            return

        # Block new connections to the database.
        self._block_new_connections.add(dbname)

        async def task():
            try:
                await self._pg_pool.prune_inactive_connections(dbname)
            except Exception:
                metrics.background_errors.inc(
                    1.0, self._instance_name, "remote_db_quarantine"
                )
                raise

        self.create_task(task(), interruptable=True)

    def on_remote_ddl(self, dbname: str) -> None:
        if not self._accept_new_tasks:
            return

        # Triggered by a postgres notification event 'schema-changes'
        # on the __edgedb_sysevent__ channel
        async def task():
            try:
                await self.introspect_db(dbname)
            except Exception:
                metrics.background_errors.inc(
                    1.0, self._instance_name, "on_remote_ddl"
                )
                raise

        self.create_task(task(), interruptable=True)

    def on_remote_database_changes(self) -> None:
        if not self._accept_new_tasks:
            return

        # Triggered by a postgres notification event 'database-changes'
        # on the __edgedb_sysevent__ channel
        async def task():
            async with self.use_sys_pgcon() as syscon:
                dbnames = set(await self._server.get_dbnames(syscon))

            tg = taskgroup.TaskGroup(name="new database introspection")
            async with tg as g:
                for dbname in dbnames:
                    if not self._dbindex.has_db(dbname):
                        g.create_task(self._early_introspect_db(dbname))

            dropped = []
            for db in self._dbindex.iter_dbs():
                if db.name not in dbnames:
                    dropped.append(db.name)
            for dbname in dropped:
                self.on_after_drop_db(dbname)

        self.create_task(task(), interruptable=True)

    def on_remote_database_config_change(self, dbname: str) -> None:
        if not self._accept_new_tasks:
            return

        # Triggered by a postgres notification event 'database-config-changes'
        # on the __edgedb_sysevent__ channel
        async def task():
            try:
                await self.introspect_db(dbname)
            except Exception:
                metrics.background_errors.inc(
                    1.0,
                    self._instance_name,
                    "on_remote_database_config_change",
                )
                raise

        self.create_task(task(), interruptable=True)

    def on_local_database_config_change(self, dbname: str) -> None:
        if not self._accept_new_tasks:
            return

        # Triggered by DB Index.
        # It's easier and safer to just schedule full re-introspection
        # of the DB and update all components of it.
        async def task():
            try:
                await self.introspect_db(dbname)
            except Exception:
                metrics.background_errors.inc(
                    1.0, self._instance_name, "on_local_database_config_change"
                )
                raise

        self.create_task(task(), interruptable=True)

    def on_remote_system_config_change(self) -> None:
        if not self._accept_new_tasks:
            return

        # Triggered by a postgres notification event 'system-config-changes'
        # on the __edgedb_sysevent__ channel

        async def task():
            try:
                cfg = await self._load_sys_config()
                self._dbindex.update_sys_config(cfg)
                self._server.reinit_idle_gc_collector()
            except Exception:
                metrics.background_errors.inc(
                    1.0, self._instance_name, "on_remote_system_config_change"
                )
                raise

        self.create_task(task(), interruptable=True)

    def on_global_schema_change(self) -> None:
        if not self._accept_new_tasks:
            return

        async def task():
            try:
                await self._reintrospect_global_schema()
            except Exception:
                metrics.background_errors.inc(
                    1.0, self._instance_name, "on_global_schema_change"
                )
                raise

        self.create_task(task(), interruptable=True)

    def get_debug_info(self) -> dict[str, Any]:
        obj = dict(
            params=dict(
                max_backend_connections=self._max_backend_connections,
                suggested_client_pool_size=self._suggested_client_pool_size,
                tenant_id=self._tenant_id,
            ),
            user_roles=self._roles,
            pg_addr={
                k: v for k, v in self.get_pgaddr().items() if k not in ["ssl"]
            },
            pg_pool=self._pg_pool._build_snapshot(now=time.monotonic()),
        )

        dbs = {}
        if self._dbindex is not None:
            for db in self._dbindex.iter_dbs():
                if db.name in defines.EDGEDB_SPECIAL_DBS:
                    continue

                dbs[db.name] = dict(
                    name=db.name,
                    dbver=db.dbver,
                    config=(
                        None
                        if db.db_config is None
                        else config.debug_serialize_config(db.db_config)
                    ),
                    extensions=sorted(db.extensions),
                    query_cache_size=db.get_query_cache_size(),
                    connections=[
                        dict(
                            in_tx=view.in_tx(),
                            in_tx_error=view.in_tx_error(),
                            config=config.debug_serialize_config(
                                view.get_session_config()),
                            module_aliases=view.get_modaliases(),
                        )
                        for view in db.iter_views()
                    ],
                )

        obj["databases"] = dbs

        return obj

    def get_compiler_args(self) -> dict[str, Any]:
        assert self._dbindex is not None
        return {"dbindex": self._dbindex}

    def iter_dbs(self) -> Iterator[dbview.Database]:
        if self._dbindex is not None:
            yield from self._dbindex.iter_dbs()
