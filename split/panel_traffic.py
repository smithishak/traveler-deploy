"""Collect observed per-profile sing-box traffic without resetting its counters."""

import datetime as dt
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import time


ENDPOINT = "127.0.0.1:19090"
DIRECTORY = Path("/var/lib/traveler-panel-traffic")
SERVICE = "traveler-origin.service"
POLL_INTERVAL = 15
COUNTER_PREFIX = "user>>>"
DIRECTIONS = {"uplink": "upload", "downlink": "download"}


def messages():
    """Create the small subset of upstream stats.proto used by this collector."""
    from google.protobuf import descriptor_pb2, message_factory

    file = descriptor_pb2.FileDescriptorProto(
        name="traveler-stats.proto", package="traveler.stats", syntax="proto3"
    )
    field = descriptor_pb2.FieldDescriptorProto
    stat = file.message_type.add(name="Stat")
    stat.field.add(name="name", number=1, type=field.TYPE_STRING)
    stat.field.add(name="value", number=2, type=field.TYPE_INT64)
    query = file.message_type.add(name="QueryStatsResponse")
    query.field.add(
        name="stat", number=1, type=field.TYPE_MESSAGE, label=field.LABEL_REPEATED,
        type_name=".traveler.stats.Stat"
    )
    system = file.message_type.add(name="SysStatsResponse")
    system.field.add(name="uptime", number=10, type=field.TYPE_UINT32)
    classes = message_factory.GetMessages([file])
    return classes["traveler.stats.QueryStatsResponse"], classes["traveler.stats.SysStatsResponse"]


class StatsClient:
    def __init__(self, channel):
        query_response, sys_response = messages()
        rpc = "/v2ray.core.app.stats.command.StatsService/"
        self.query = channel.unary_unary(
            rpc + "QueryStats", response_deserializer=query_response.FromString
        )
        self.system = channel.unary_unary(
            rpc + "GetSysStats", response_deserializer=sys_response.FromString
        )

    def sample(self):
        before = invocation()
        uptime_before = self.system(b"", timeout=3).uptime
        response = self.query(b"", timeout=3)
        uptime = self.system(b"", timeout=3).uptime
        after = invocation()
        if before != after or uptime < uptime_before:
            raise RuntimeError("sing-box restarted during collection")
        counters = {}
        for stat in response.stat:
            if not stat.name.startswith(COUNTER_PREFIX) or stat.value < 0:
                continue
            name, separator, key = stat.name[len(COUNTER_PREFIX):].rpartition(">>>traffic>>>")
            if name and separator and key in DIRECTIONS:
                counters.setdefault(name, {})[DIRECTIONS[key]] = stat.value
        return after, uptime, counters


def invocation():
    result = subprocess.run(
        ["systemctl", "show", "--property=InvocationID", "--value", SERVICE],
        capture_output=True, text=True, check=True, timeout=3
    ).stdout.strip()
    if len(result) != 32 or not result.strip("0"):
        raise RuntimeError("sing-box has no active systemd invocation")
    return result


def update(state, generation, uptime, counters, now, sampled_at):
    # A SIGHUP can replace the stats service without changing InvocationID.
    # Use its uptime alongside the invocation to detect that reset.
    same_service = (
        state.get("generation") == generation
        and abs((uptime - state["uptime"]) - (sampled_at - state["sampled_at"])) <= 2
    )
    previous = state["raw"] if same_service else {}
    users = {name: dict(value) for name, value in state["users"].items()}
    for name, values in counters.items():
        item = users.setdefault(name, {"last_seen": None, "upload": 0, "download": 0})
        baseline = previous.get(name, {})
        for direction, value in values.items():
            last = baseline.get(direction, 0)
            # An administrator can also explicitly reset individual counters.
            delta = value - last if value >= last else value
            if delta:
                item[direction] += delta
                item["last_seen"] = now
    return {
        "generation": generation, "uptime": uptime, "sampled_at": sampled_at,
        "raw": counters, "users": users,
    }


def write_json(path, data, durable=False):
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".traffic-", delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            json.dump(data, file, ensure_ascii=False, separators=(",", ":"))
            file.write("\n")
            file.flush()
            if durable:
                os.fsync(file.fileno())
            file.close()
            temporary.replace(path)
            if durable:
                descriptor = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        finally:
            temporary.unlink(missing_ok=True)


def load_state(path):
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {"raw": {}, "users": {}}


def main():
    import grpc

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    state_path = DIRECTORY / "collector.json"
    public_path = DIRECTORY / "stats.json"
    state = load_state(state_path)
    reachable = None
    with grpc.insecure_channel(ENDPOINT, options=[("grpc.enable_http_proxy", 0)]) as channel:
        client = StatsClient(channel)
        while True:
            try:
                generation, uptime, counters = client.sample()
            except (grpc.RpcError, subprocess.SubprocessError, RuntimeError, OSError) as error:
                if reachable is not False:
                    reason = error.code().name if isinstance(error, grpc.RpcError) else type(error).__name__
                    logging.warning("sing-box statistics unavailable (%s); waiting for the local API", reason)
                reachable = False
            else:
                if reachable is not True:
                    logging.info("sing-box statistics available")
                reachable = True
                now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
                next_state = update(state, generation, uptime, counters, now, time.monotonic())
                if any(next_state[key] != state.get(key) for key in ("generation", "raw", "users")):
                    write_json(state_path, next_state, durable=True)
                state = next_state
                write_json(public_path, {"updated_at": now, "users": state["users"]})
            time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
