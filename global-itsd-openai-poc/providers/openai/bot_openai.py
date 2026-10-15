
    # ---------------------------------------------------------------- shutdown

    async def _shutdown_conversation(self, convo: BotConversation) -> None:
        """Tear down every per-conversation resource: drain tasks, ingress streamer, OpenAI WS.

        Contract:
            Idempotent shutdown for a single `BotConversation`.
            Called on every exit path: `_handle_bot_end`,
            `on_session_ended`, and `shutdown` (process
            shutdown). Marks `convo.active = False` first so any
            in-flight `_openai_recv_loop` iteration sees the
            flag and exits at its next message boundary.

            Order of cleanup:
                1. Cancel the **handoff drain task**
                   (`handoff_task`) if still running. A caller
                   hangup mid-handoff-wait means Infinity is
                   already tearing the session down — there is
                   nothing to drain to and nothing useful to
                   emit. Cancel without emitting; do not invoke
                   `_emit_pending_handoff` from this path.
                   Clears `pending_handoff` and
                   `pending_handoff_args`.
                2. Cancel the **session-end drain task**
                   (`session_end_task`) symmetrically. Same
                   reasoning: a caller hangup mid-session-end-
                   wait means the success-context `bot.ended`
                   is moot. Clears `pending_session_end`.
                3. Close the **conv_ mirror** (`conv_mirror`) if
                   present: drain pending item POSTs under a
                   bounded timeout, then cancel its worker.
                   Best-effort; timed-out items are dropped.
                4. Drain and tear down the IngressStreamer for
                   this `(session_id, endpoint_id)` via
                   `stop_and_clear`. Purges the per-endpoint
                   queue and cancels the streaming task.
                5. Cancel the OpenAI receive loop task and
                   await its exit so no further events are
                   dispatched after this point.
                6. Close the upstream OpenAI WebSocket. Errors
                   here are logged at DEBUG and swallowed —
                   the connection is being torn down anyway.

            Does NOT emit `bot.ended`. The disconnect-context
            emit lives in `on_session_ended` (caller-driven
            path); the bot.end ack lives in `_handle_bot_end`.
            Both call this method *after* their respective
            bot.ended emissions.

        Args:
            convo: The conversation to tear down. Mutated in
                place: every resource attribute is cleared or
                cancelled.

        Returns:
            None. All exceptions raised by cleanup steps are
            logged and swallowed; the method always returns
            cleanly.
        """
        convo.active = False

        handoff_task = convo.handoff_task
        convo.handoff_task = None
        convo.pending_handoff = None
        convo.pending_handoff_args = None
        if handoff_task and not handoff_task.done():
            handoff_task.cancel()
            try:
                await handoff_task
            except (asyncio.CancelledError, Exception):
                pass

        session_end_task = convo.session_end_task
        convo.session_end_task = None
        convo.pending_session_end = False
        if session_end_task and not session_end_task.done():
            session_end_task.cancel()
            try:
                await session_end_task
            except (asyncio.CancelledError, Exception):
                pass

        # Drain any pending conv_ mirror posts (bounded timeout), then cancel
        # the worker. Best-effort: timed-out items are dropped with a WARNING.
        mirror = convo.conv_mirror
        convo.conv_mirror = None
        if mirror is not None:
            await mirror.aclose()

        if convo.endpoint_id:
            try:
                await self.server.ingress_streamer.stop_and_clear(
                    convo.session_id, convo.endpoint_id
                )
            except Exception as exc:
                logger.debug("[%s] stop_and_clear raised: %s", convo.client_id, exc)

        task = convo.oa_recv_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        ws = convo.oa_ws
        convo.oa_ws = None
        if ws:
            try:
                await ws.close()
            except Exception as exc:
                logger.debug("[%s] OpenAI ws.close raised: %s", convo.client_id, exc)


def register(server: "BridgeServer") -> OpenAIService:
    """Plugin entrypoint — instantiate `OpenAIService` and register it with the bridge.

    Discovered and called by `bot_service.py` at startup if
    `OpenAIService.is_configured()` returns `True` (i.e.
    `OPENAI_API_KEY` is set in the environment). The constructed
    plugin is added to the bridge's `ServiceRegistry` and
    thereafter receives every `bot.start` / `bot.end` whose
    `payload.botId` carries the `openai:` prefix.

    Args:
        server: The owning `BridgeServer` instance.
