"""Billable provider check; prints only status and byte counts."""
import asyncio
from datetime import datetime, timedelta, timezone
import os
import random
import struct
from pathlib import Path
import sys
import wave

from google import genai

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from voice import live_config


async def check_flows(session):
    """Optional billable evaluation; delayed fixture, no booking writes or real microphone."""
    await session.send_client_content(turns={"role": "user", "parts": [{"text": "What is the general checked baggage policy?"}]}, turn_complete=True)
    final_sent = finished = False
    progress_audio = 0
    acknowledgments = 0
    before_result_text = ""
    completion = None
    notes_saved = False

    async def finish(fc):
        nonlocal final_sent
        await asyncio.sleep(8)  # Prove speech continues while the lookup is outstanding.
        await session.send_tool_response(function_responses={"id": fc.id, "name": fc.name,
            "will_continue": False, "scheduling": "WHEN_IDLE", "response": {"result": {
                "status": "snapshot", "facts": ["Checked bag fees depend on the route and fare. Check the ticket's allowance."],
                "title": "Baggage policy", "reviewedAt": "2026-10-07"}}})
        final_sent = True

    try:
        async with asyncio.timeout(35):
            while True:
                async for message in session.receive():
                    if message.tool_call:
                        for fc in message.tool_call.function_calls:
                            if fc.name == "save_notes":
                                if not fc.args.get("notes", "").strip():
                                    raise RuntimeError("Empty customer interaction note.")
                                notes_saved = True
                                await session.send_tool_response(function_responses={"id": fc.id, "name": fc.name,
                                    "will_continue": False, "scheduling": "SILENT", "response": {"result": {"saved": True}}})
                                continue
                            if fc.name != "lookup_policy" or completion:
                                raise RuntimeError("Unexpected or duplicate policy tool call.")
                            await session.send_tool_response(function_responses={"id": fc.id, "name": fc.name,
                                "will_continue": True, "scheduling": "SILENT", "response": {"result": {"status": "in_progress"}}})
                            completion = asyncio.create_task(finish(fc))
                    if message.data and not final_sent:
                        progress_audio += len(message.data)
                    if message.server_content and message.server_content.output_transcription and not final_sent:
                        before_result_text += message.server_content.output_transcription.text or ""
                    if message.server_content and message.server_content.turn_complete and not final_sent:
                        acknowledgments += 1
                    if message.server_content and message.server_content.turn_complete and final_sent:
                        finished = True
                        break
                if finished:
                    break
        if not progress_audio or not completion or acknowledgments != 1:
            raise RuntimeError(f"Expected one spoken acknowledgment before the policy result; turns={acknowledgments}, audio_bytes={progress_audio}, text={before_result_text!r}.")
        await completion
        print("PASS: exactly one acknowledgment during delayed async policy lookup")
        if not notes_saved:
            raise RuntimeError("Agent did not take notes for the policy inquiry.")
        print("PASS: agent silently saves customer interaction notes on a policy-only call")
    finally:
        if completion and not completion.done():
            completion.cancel()
            await asyncio.gather(completion, return_exceptions=True)

    await session.send_client_content(turns={"role": "user", "parts": [{"text": "My confirmation number is R2R82A. Please read it back."}]}, turn_complete=True)
    pcm, transcript = bytearray(), ""
    async for message in session.receive():
        if message.data:
            pcm.extend(message.data)
        if message.server_content and message.server_content.output_transcription:
            transcript += message.server_content.output_transcription.text or ""
    if transcript.lower().count("romeo") != 2 or "alpha" not in transcript.lower() or "as in" in transcript.lower() or not pcm:
        raise RuntimeError("NATO readback evaluation failed.")
    target = Path(__file__).resolve().parents[1] / "artifacts" / "readback.wav"
    target.parent.mkdir(exist_ok=True)
    with wave.open(str(target), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(pcm)
    print("PASS: NATO readback transcript; recorded audio in artifacts/readback.wav for listening")


async def check_waiting(session):
    async def expect_silence(seconds):
        try:
            async with asyncio.timeout(seconds):
                while True:
                    async for message in session.receive():
                        if message.data or message.tool_call:
                            raise RuntimeError("Agent spoke or called a tool during a thinking/noise interval.")
        except TimeoutError:
            pass

    await session.send_client_content(turns={"role": "user", "parts": [{"text": "Ummmmmmmm... let me think."}]}, turn_complete=True)
    await expect_silence(5)
    rng = random.Random(42)
    async def noise():
        for _ in range(20):
            pcm = struct.pack("<1600h", *(rng.randint(-450, 450) for _ in range(1600)))
            await session.send_realtime_input(audio={"data": pcm, "mime_type": "audio/pcm;rate=16000"})
            await asyncio.sleep(.1)
        await session.send_realtime_input(audio_stream_end=True)
    await asyncio.gather(noise(), expect_silence(7))
    await session.send_client_content(turns={"role": "user", "parts": [{"text": "I'm ready now. Please repeat the confirmation number."}]}, turn_complete=True)
    async def receive_resume():
        heard = False
        async for message in session.receive():
            heard |= bool(message.data)
            if message.server_content and message.server_content.interrupted:
                raise RuntimeError("Synthetic static interrupted the spoken reply.")
        return heard
    _, heard = await asyncio.gather(noise(), receive_resume())
    if not heard:
        raise RuntimeError("Agent did not resume after a meaningful request.")
    print("PASS: silent for thinking request/static; resumes for meaningful request without noise interruption")


async def main():
    if not os.getenv("GEMINI_API_KEY"):
        raise ValueError("Set GEMINI_API_KEY in .env.")
    model, config = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live"), live_config()
    async with asyncio.timeout(90 if "--flows" in sys.argv else 40):
        with genai.Client(api_key=os.environ["GEMINI_API_KEY"], http_options={"api_version": "v1beta", "timeout": 15_000}) as server:
            now = datetime.now(timezone.utc)
            auth = await server.aio.auth_tokens.create(config={"uses": 1, "expire_time": now + timedelta(minutes=30), "new_session_expire_time": now + timedelta(minutes=1), "live_connect_constraints": {"model": model, "config": config}})
        print("PASS: constrained ephemeral token created")
        with genai.Client(api_key=auth.name, http_options={"api_version": "v1beta"}) as client:
            async with client.aio.live.connect(model=model, config=config) as session:
                await session.send_client_content(turns={"role": "user", "parts": [{"text": "Briefly introduce this demo in one sentence."}]}, turn_complete=True)
                count = 0
                async for message in session.receive():
                    if message.data:
                        count += len(message.data)
                    if message.server_content and message.server_content.turn_complete:
                        break
                if not count:
                    raise RuntimeError("No voice audio received.")
                print(f"PASS: {model} returned {count} PCM audio bytes")
                if "--flows" in sys.argv:
                    await check_flows(session)
                    await check_waiting(session)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except RuntimeError as error:
        print(f"FAIL: {error}", file=sys.stderr)
        sys.exit(1)
    except Exception:
        print("FAIL: voice service check failed; verify model availability, key permissions and network access.", file=sys.stderr)
        sys.exit(1)
