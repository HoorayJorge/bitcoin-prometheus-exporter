#!/usr/bin/env python3
# garlicoind-monitor.py
#
# An exporter for Prometheus and Bitcoin Core.
#
# Copyright 2018 Kevin M. Gallagher
# Copyright 2019,2020 Jeff Stein
#
# Published at https://github.com/jvstein/garlicoin-prometheus-exporter
# Licensed under BSD 3-clause (see LICENSE).
#
# Dependency licenses (retrieved 2020-05-31):
#   prometheus_client: Apache 2.0
#   python-garlicoinlib: LGPLv3
#   riprova: MIT

import json
import logging
import time
import os
import signal
import sys
import socket
from decimal import Decimal
from datetime import datetime
from functools import lru_cache
from typing import Any
from typing import Dict
from typing import List
from typing import Union
from wsgiref.simple_server import make_server

import riprova

from garlicoin.rpc import JSONRPCError, InWarmupError, Proxy
from prometheus_client import make_wsgi_app, Gauge, Counter


logger = logging.getLogger("garlicoin-exporter")

# Create Prometheus metrics to track garlicoind stats.
GARLICOIN_BLOCKS = Gauge("garlicoind_blocks", "Block height")
GARLICOIN_DIFFICULTY = Gauge("garlicoind_difficulty", "Difficulty")
GARLICOIN_PEERS = Gauge("garlicoind_peers", "Number of peers")
GARLICOIN_CONN_IN = Gauge("garlicoind_conn_in", "Number of connections in")
GARLICOIN_CONN_OUT = Gauge("garlicoind_conn_out", "Number of connections out")

GARLICOIN_HASHPS_GAUGES = {}  # type: Dict[int, Gauge]
GARLICOIN_ESTIMATED_SMART_FEE_GAUGES = {}  # type: Dict[int, Gauge]

GARLICOIN_WARNINGS = Counter("garlicoind_warnings", "Number of network or blockchain warnings detected")
GARLICOIN_UPTIME = Gauge("garlicoind_uptime", "Number of seconds the Bitcoin daemon has been running")

GARLICOIN_MEMINFO_USED = Gauge("garlicoind_meminfo_used", "Number of bytes used")
GARLICOIN_MEMINFO_FREE = Gauge("garlicoind_meminfo_free", "Number of bytes available")
GARLICOIN_MEMINFO_TOTAL = Gauge("garlicoind_meminfo_total", "Number of bytes managed")
GARLICOIN_MEMINFO_LOCKED = Gauge("garlicoind_meminfo_locked", "Number of bytes locked")
GARLICOIN_MEMINFO_CHUNKS_USED = Gauge("garlicoind_meminfo_chunks_used", "Number of allocated chunks")
GARLICOIN_MEMINFO_CHUNKS_FREE = Gauge("garlicoind_meminfo_chunks_free", "Number of unused chunks")

GARLICOIN_MEMPOOL_BYTES = Gauge("garlicoind_mempool_bytes", "Size of mempool in bytes")
GARLICOIN_MEMPOOL_SIZE = Gauge(
    "garlicoind_mempool_size", "Number of unconfirmed transactions in mempool"
)
GARLICOIN_MEMPOOL_USAGE = Gauge("garlicoind_mempool_usage", "Total memory usage for the mempool")
GARLICOIN_MEMPOOL_UNBROADCAST = Gauge(
    "garlicoind_mempool_unbroadcast", "Number of transactions waiting for acknowledgment"
)

GARLICOIN_LATEST_BLOCK_HEIGHT = Gauge(
    "garlicoind_latest_block_height", "Height or index of latest block"
)
GARLICOIN_LATEST_BLOCK_WEIGHT = Gauge(
    "garlicoind_latest_block_weight", "Weight of latest block according to BIP 141"
)
GARLICOIN_LATEST_BLOCK_SIZE = Gauge("garlicoind_latest_block_size", "Size of latest block in bytes")
GARLICOIN_LATEST_BLOCK_TXS = Gauge(
    "garlicoind_latest_block_txs", "Number of transactions in latest block"
)

GARLICOIN_TXCOUNT = Gauge("garlicoind_txcount", "Number of TX since the genesis block")

GARLICOIN_NUM_CHAINTIPS = Gauge("garlicoind_num_chaintips", "Number of known blockchain branches")

GARLICOIN_TOTAL_BYTES_RECV = Gauge("garlicoind_total_bytes_recv", "Total bytes received")
GARLICOIN_TOTAL_BYTES_SENT = Gauge("garlicoind_total_bytes_sent", "Total bytes sent")

GARLICOIN_LATEST_BLOCK_INPUTS = Gauge(
    "garlicoind_latest_block_inputs", "Number of inputs in transactions of latest block"
)
GARLICOIN_LATEST_BLOCK_OUTPUTS = Gauge(
    "garlicoind_latest_block_outputs", "Number of outputs in transactions of latest block"
)
GARLICOIN_LATEST_BLOCK_VALUE = Gauge(
    "garlicoind_latest_block_value", "Bitcoin value of all transactions in the latest block"
)
GARLICOIN_LATEST_BLOCK_FEE = Gauge(
    "garlicoind_latest_block_fee", "Total fee to process the latest block"
)

GARLICOIN_BAN_CREATED = Gauge(
    "garlicoind_ban_created", "Time the ban was created", labelnames=["address", "reason"]
)
GARLICOIN_BANNED_UNTIL = Gauge(
    "garlicoind_banned_until", "Time the ban expires", labelnames=["address", "reason"]
)

GARLICOIN_SERVER_VERSION = Gauge("garlicoind_server_version", "The server version")
GARLICOIN_PROTOCOL_VERSION = Gauge("garlicoind_protocol_version", "The protocol version of the server")

GARLICOIN_SIZE_ON_DISK = Gauge("garlicoind_size_on_disk", "Estimated size of the block and undo files")

GARLICOIN_VERIFICATION_PROGRESS = Gauge(
    "garlicoind_verification_progress", "Estimate of verification progress [0..1]"
)

GARLICOIN_RPC_ACTIVE = Gauge("garlicoind_rpc_active", "Number of RPC calls being processed")

EXPORTER_ERRORS = Counter(
    "garlicoind_exporter_errors", "Number of errors encountered by the exporter", labelnames=["type"]
)
PROCESS_TIME = Counter(
    "garlicoind_exporter_process_time", "Time spent processing metrics from garlicoin node"
)

SATS_PER_COIN = Decimal(1e8)

GARLICOIN_RPC_SCHEME = os.environ.get("GARLICOIN_RPC_SCHEME", "http")
GARLICOIN_RPC_HOST = os.environ.get("GARLICOIN_RPC_HOST", "localhost")
GARLICOIN_RPC_PORT = os.environ.get("GARLICOIN_RPC_PORT", "8332")
GARLICOIN_RPC_USER = os.environ.get("GARLICOIN_RPC_USER")
GARLICOIN_RPC_PASSWORD = os.environ.get("GARLICOIN_RPC_PASSWORD")
GARLICOIN_CONF_PATH = os.environ.get("GARLICOIN_CONF_PATH")
HASHPS_BLOCKS = [int(b) for b in os.environ.get("HASHPS_BLOCKS", "-1,1,120").split(",") if b != ""]
SMART_FEES = [int(f) for f in os.environ.get("SMARTFEE_BLOCKS", "2,3,5,20").split(",") if f != ""]
METRICS_ADDR = os.environ.get("METRICS_ADDR", "")  # empty = any address
METRICS_PORT = int(os.environ.get("METRICS_PORT", "9332"))
RETRIES = int(os.environ.get("RETRIES", 5))
TIMEOUT = int(os.environ.get("TIMEOUT", 30))
RATE_LIMIT_SECONDS = int(os.environ.get("RATE_LIMIT", 5))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")


RETRY_EXCEPTIONS = (InWarmupError, ConnectionError, socket.timeout)

RpcResult = Union[Dict[str, Any], List[Any], str, int, float, bool, None]


def on_retry(err: Exception, next_try: float) -> None:
    err_type = type(err)
    exception_name = err_type.__module__ + "." + err_type.__name__
    EXPORTER_ERRORS.labels(**{"type": exception_name}).inc()
    logger.error("Retry after exception %s: %s", exception_name, err)


def error_evaluator(e: Exception) -> bool:
    return isinstance(e, RETRY_EXCEPTIONS)


@lru_cache(maxsize=1)
def rpc_client_factory():
    # Configuration is done in this order of precedence:
    #   - Explicit config file.
    #   - GARLICOIN_RPC_USER and GARLICOIN_RPC_PASSWORD environment variables.
    #   - Default garlicoin config file (as handled by Proxy.__init__).
    use_conf = (
        (GARLICOIN_CONF_PATH is not None)
        or (GARLICOIN_RPC_USER is None)
        or (GARLICOIN_RPC_PASSWORD is None)
    )

    if use_conf:
        logger.info("Using config file: %s", GARLICOIN_CONF_PATH or "<default>")
        return lambda: Proxy(btc_conf_file=GARLICOIN_CONF_PATH, timeout=TIMEOUT)
    else:
        host = GARLICOIN_RPC_HOST
        host = "{}:{}@{}".format(GARLICOIN_RPC_USER, GARLICOIN_RPC_PASSWORD, host)
        if GARLICOIN_RPC_PORT:
            host = "{}:{}".format(host, GARLICOIN_RPC_PORT)
        service_url = "{}://{}".format(GARLICOIN_RPC_SCHEME, host)
        logger.info("Using environment configuration")
        return lambda: Proxy(service_url=service_url, timeout=TIMEOUT)


def rpc_client():
    return rpc_client_factory()()


@riprova.retry(
    timeout=TIMEOUT,
    backoff=riprova.ExponentialBackOff(),
    on_retry=on_retry,
    error_evaluator=error_evaluator,
)
def garlicoinrpc(*args) -> RpcResult:
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("RPC call: " + " ".join(str(a) for a in args))

    result = rpc_client().call(*args)

    logger.debug("Result:   %s", result)
    return result


@lru_cache(maxsize=1)
def getblockstats(block_hash: str):
    try:
        block = garlicoinrpc(
            "getblockstats",
            block_hash,
            ["total_size", "total_weight", "totalfee", "txs", "height", "ins", "outs", "total_out"],
        )
    except Exception:
        logger.exception("Failed to retrieve block " + block_hash + " statistics from garlicoind.")
        return None
    return block


def smartfee_gauge(num_blocks: int) -> Gauge:
    gauge = GARLICOIN_ESTIMATED_SMART_FEE_GAUGES.get(num_blocks)
    if gauge is None:
        gauge = Gauge(
            "garlicoind_est_smart_fee_%d" % num_blocks,
            "Estimated smart fee per kilobyte for confirmation in %d blocks" % num_blocks,
        )
        GARLICOIN_ESTIMATED_SMART_FEE_GAUGES[num_blocks] = gauge
    return gauge


def do_smartfee(num_blocks: int) -> None:
    smartfee = garlicoinrpc("estimatesmartfee", num_blocks).get("feerate")
    if smartfee is not None:
        gauge = smartfee_gauge(num_blocks)
        gauge.set(smartfee)


def hashps_gauge_suffix(nblocks):
    if nblocks < 0:
        return "_neg%d" % -nblocks
    if nblocks == 120:
        return ""
    return "_%d" % nblocks


def hashps_gauge(num_blocks: int) -> Gauge:
    gauge = GARLICOIN_HASHPS_GAUGES.get(num_blocks)
    if gauge is None:
        desc_end = "for the last %d blocks" % num_blocks
        if num_blocks == -1:
            desc_end = "since the last difficulty change"
        gauge = Gauge(
            "garlicoind_hashps%s" % hashps_gauge_suffix(num_blocks),
            "Estimated network hash rate per second %s" % desc_end,
        )
        GARLICOIN_HASHPS_GAUGES[num_blocks] = gauge
    return gauge


def do_hashps_gauge(num_blocks: int) -> None:
    hps = float(garlicoinrpc("getnetworkhashps", num_blocks))
    if hps is not None:
        gauge = hashps_gauge(num_blocks)
        gauge.set(hps)


def refresh_metrics() -> None:
    uptime = int(garlicoinrpc("uptime"))
    meminfo = garlicoinrpc("getmemoryinfo", "stats")["locked"]
    blockchaininfo = garlicoinrpc("getblockchaininfo")
    networkinfo = garlicoinrpc("getnetworkinfo")
    chaintips = len(garlicoinrpc("getchaintips"))
    mempool = garlicoinrpc("getmempoolinfo")
    nettotals = garlicoinrpc("getnettotals")
    rpcinfo = garlicoinrpc("getrpcinfo")
    txstats = garlicoinrpc("getchaintxstats")
    latest_blockstats = getblockstats(str(blockchaininfo["bestblockhash"]))

    banned = garlicoinrpc("listbanned")

    GARLICOIN_UPTIME.set(uptime)
    GARLICOIN_BLOCKS.set(blockchaininfo["blocks"])
    GARLICOIN_PEERS.set(networkinfo["connections"])
    if "connections_in" in networkinfo:
        GARLICOIN_CONN_IN.set(networkinfo["connections_in"])
    if "connections_out" in networkinfo:
        GARLICOIN_CONN_OUT.set(networkinfo["connections_out"])
    GARLICOIN_DIFFICULTY.set(blockchaininfo["difficulty"])

    GARLICOIN_SERVER_VERSION.set(networkinfo["version"])
    GARLICOIN_PROTOCOL_VERSION.set(networkinfo["protocolversion"])
    GARLICOIN_SIZE_ON_DISK.set(blockchaininfo["size_on_disk"])
    GARLICOIN_VERIFICATION_PROGRESS.set(blockchaininfo["verificationprogress"])

    for smartfee in SMART_FEES:
        do_smartfee(smartfee)

    for hashps_block in HASHPS_BLOCKS:
        do_hashps_gauge(hashps_block)

    for ban in banned:
        GARLICOIN_BAN_CREATED.labels(
            address=ban["address"], reason=ban.get("ban_reason", "manually added")
        ).set(ban["ban_created"])
        GARLICOIN_BANNED_UNTIL.labels(
            address=ban["address"], reason=ban.get("ban_reason", "manually added")
        ).set(ban["banned_until"])

    if networkinfo["warnings"]:
        GARLICOIN_WARNINGS.inc()

    GARLICOIN_TXCOUNT.set(txstats["txcount"])

    GARLICOIN_NUM_CHAINTIPS.set(chaintips)

    GARLICOIN_MEMINFO_USED.set(meminfo["used"])
    GARLICOIN_MEMINFO_FREE.set(meminfo["free"])
    GARLICOIN_MEMINFO_TOTAL.set(meminfo["total"])
    GARLICOIN_MEMINFO_LOCKED.set(meminfo["locked"])
    GARLICOIN_MEMINFO_CHUNKS_USED.set(meminfo["chunks_used"])
    GARLICOIN_MEMINFO_CHUNKS_FREE.set(meminfo["chunks_free"])

    GARLICOIN_MEMPOOL_BYTES.set(mempool["bytes"])
    GARLICOIN_MEMPOOL_SIZE.set(mempool["size"])
    GARLICOIN_MEMPOOL_USAGE.set(mempool["usage"])
    if "unbroadcastcount" in mempool:
        GARLICOIN_MEMPOOL_UNBROADCAST.set(mempool["unbroadcastcount"])

    GARLICOIN_TOTAL_BYTES_RECV.set(nettotals["totalbytesrecv"])
    GARLICOIN_TOTAL_BYTES_SENT.set(nettotals["totalbytessent"])

    if latest_blockstats is not None:
        GARLICOIN_LATEST_BLOCK_SIZE.set(latest_blockstats["total_size"])
        GARLICOIN_LATEST_BLOCK_TXS.set(latest_blockstats["txs"])
        GARLICOIN_LATEST_BLOCK_HEIGHT.set(latest_blockstats["height"])
        GARLICOIN_LATEST_BLOCK_WEIGHT.set(latest_blockstats["total_weight"])
        GARLICOIN_LATEST_BLOCK_INPUTS.set(latest_blockstats["ins"])
        GARLICOIN_LATEST_BLOCK_OUTPUTS.set(latest_blockstats["outs"])
        GARLICOIN_LATEST_BLOCK_VALUE.set(latest_blockstats["total_out"] / SATS_PER_COIN)
        GARLICOIN_LATEST_BLOCK_FEE.set(latest_blockstats["totalfee"] / SATS_PER_COIN)

    # Subtract one because we don't want to count the "getrpcinfo" call itself
    GARLICOIN_RPC_ACTIVE.set(len(rpcinfo["active_commands"]) - 1)


def sigterm_handler(signal, frame) -> None:
    logger.critical("Received SIGTERM. Exiting.")
    sys.exit(0)


def exception_count(e: Exception) -> None:
    err_type = type(e)
    exception_name = err_type.__module__ + "." + err_type.__name__
    EXPORTER_ERRORS.labels(**{"type": exception_name}).inc()


def main():
    # Set up logging to look similar to garlicoin logs (UTC).
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"
    )
    logging.Formatter.converter = time.gmtime
    logger.setLevel(LOG_LEVEL)

    # Handle SIGTERM gracefully.
    signal.signal(signal.SIGTERM, sigterm_handler)

    app = make_wsgi_app()

    last_refresh = datetime.fromtimestamp(0)

    def refresh_app(*args, **kwargs):
        nonlocal last_refresh
        process_start = datetime.now()

        # Only refresh every RATE_LIMIT_SECONDS seconds.
        if (process_start - last_refresh).total_seconds() < RATE_LIMIT_SECONDS:
            return app(*args, **kwargs)

        # Allow riprova.MaxRetriesExceeded and unknown exceptions to crash the process.
        try:
            refresh_metrics()
        except riprova.exceptions.RetryError as e:
            logger.error("Refresh failed during retry. Cause: " + str(e))
            exception_count(e)
        except JSONRPCError as e:
            logger.debug("Bitcoin RPC error refresh", exc_info=True)
            exception_count(e)
        except json.decoder.JSONDecodeError as e:
            logger.error("RPC call did not return JSON. Bad credentials? " + str(e))
            sys.exit(1)

        duration = datetime.now() - process_start
        PROCESS_TIME.inc(duration.total_seconds())
        logger.info("Refresh took %s seconds", duration)
        last_refresh = process_start

        return app(*args, **kwargs)

    httpd = make_server(METRICS_ADDR, METRICS_PORT, refresh_app)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
