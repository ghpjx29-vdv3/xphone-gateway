"""Parse Muse completion reports and inspect Worker delivery status."""

import json
import uuid


def batch_id(pack_id: str, playthrough_id: str, event_id: str, step: int = 0) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"xphone:{pack_id}:{playthrough_id}:{event_id}:{step}"))


def delivery_status(mcp_call, batch: str) -> str:
    result = mcp_call("xphone.get_delivery", {"batchId": batch})
    return json.loads(result["content"][0]["text"])["status"]
