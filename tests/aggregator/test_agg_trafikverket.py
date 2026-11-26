"""Trafikverket's deviations on the aggregator: storage and API."""

from aggregator import api
from aggregator.pipeline import Pipeline


def situation():
    return {"Id": "S1", "Deviation": [{
        "Id": "D1", "MessageType": "Olycka", "RoadNumber": "E18",
        "StartTime": "2026-11-26T13:02:00.000+01:00",
        "Geometry": {"Point": {"WGS84": "POINT (13.43247 59.379 0)"}}}]}


async def test_aggregator_api_and_status(aiohttp_client, agg_store):
    agg_store.trv.apply([situation()])
    pipe = Pipeline(transcriber=None, mqtt=None, geo=None)
    client = await aiohttp_client(api.create_web_app(pipe))
    data = await (await client.get("/api/v1/trafikverket")).json()
    assert data["deviations"][0]["id"] == "D1"
    assert data["deviations"][0]["start_time"] == "2026-11-26T12:02:00Z"
    status = await (await client.get("/api/v1/status")).json()
    assert status["trafikverket"]["enabled"] is False
