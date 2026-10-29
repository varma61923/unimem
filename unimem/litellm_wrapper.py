"""LiteLLM proxy wrapper providing zero-code auto-recall and auto-extraction memory integration.

Adapted from Mem0 proxy architecture:
- Automatically recalls relevant memory context from UniMem before invoking LLM completions.
- Injects recalled context seamlessly into prompt/system messages within configured token budgets.
- Captures completed conversation turns and extracts profile facts through the shared store.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


class UniMemLiteLLM:
    """Wrapper around LiteLLM completions with automated UniMem memory augmentations."""

    def __init__(
        self,
        root: str = ".",
        *,
        token_budget: int = 1500,
        auto_extract: bool = True,
        space: str = "default",
        session: str = "litellm",
    ) -> None:
        self.root = root
        self.token_budget = token_budget
        self.auto_extract = auto_extract
        self.space = space
        self.session = session

    def _extract_last_user_message(self, messages: list[dict[str, Any]]) -> str:
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    texts = [
                        item.get("text", "")
                        for item in content
                        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
                    ]
                    return " ".join(texts)
        return ""

    def _augment_messages(
        self,
        messages: list[dict[str, Any]],
        context_text: str,
    ) -> list[dict[str, Any]]:
        if not context_text:
            return list(messages)

        from unimem import injection_guard

        memory_block = f"[UniMem Memory Context]\n{injection_guard.NOTICE}\n{context_text}\n[End Memory Context]"

        augmented = []
        injected = False
        for msg in messages:
            if not injected and msg.get("role") == "system":
                # Augment existing system message
                original = msg.get("content", "")
                content = (
                    [*original, {"type": "text", "text": memory_block}]
                    if isinstance(original, list)
                    else f"{original}\n\n{memory_block}"
                    if original
                    else memory_block
                )
                augmented.append(
                    {
                        **msg,
                        "content": content,
                    }
                )
                injected = True
            else:
                augmented.append(dict(msg))

        if not injected:
            # Prepend a new system message
            augmented.insert(0, {"role": "system", "content": memory_block})

        return augmented

    def completion(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        completion_fn: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        """Execute LLM completion with automatic memory recall and persistence."""
        user_query = self._extract_last_user_message(messages)
        recalled_text = ""

        # Step 1: Auto-Recall relevant memories
        if user_query:
            try:
                from unimem.conversation import Options, Store
                from unimem.conversation import recall as conv_recall

                with Store(self.root, self.space, create=False, read_only=True) as store:
                    recalled = conv_recall(store, user_query, options=Options(budget=self.token_budget))
                    recalled_text = recalled.context
            except Exception as exc:
                from unimem.conversation import ConversationError

                if isinstance(exc, ConversationError) and "no conversations stored" in str(exc):
                    logger.debug("Auto-recall skipped: no conversation memory exists yet")
                else:
                    logger.warning("Auto-recall failed (%s)", type(exc).__name__)

        augmented_messages = self._augment_messages(messages, recalled_text)

        # Step 2: Invoke Completion
        if completion_fn is not None:
            response = completion_fn(model=model, messages=augmented_messages, **kwargs)
        else:
            try:
                import litellm

                response = litellm.completion(model=model, messages=augmented_messages, **kwargs)
            except ImportError:
                raise RuntimeError(
                    "litellm is required to use UniMemLiteLLM without a custom completion_fn. "
                    "Install it via `pip install litellm`."
                ) from None

        # Step 3: Auto-Extract conversation turns
        if self.auto_extract and user_query:
            try:
                assistant_reply = ""
                if hasattr(response, "choices") and response.choices:
                    choice = response.choices[0]
                    if hasattr(choice, "message"):
                        assistant_reply = getattr(choice.message, "content", "") or ""
                elif isinstance(response, dict):
                    choices = response.get("choices", [])
                    if choices:
                        assistant_reply = choices[0].get("message", {}).get("content", "")

                if isinstance(assistant_reply, str) and assistant_reply:
                    from unimem.conversation import Store

                    with Store(self.root, self.space) as store:
                        store.add(
                            self.session,
                            [
                                {"speaker": "user", "role": "user", "text": user_query},
                                {"speaker": "assistant", "role": "assistant", "text": assistant_reply},
                            ],
                            user_speakers=("user",),
                        )
            except Exception as exc:
                logger.warning("Auto-extraction failed (%s)", type(exc).__name__)

        return response


def wrap_completion(
    root: str = ".",
    *,
    token_budget: int = 1500,
    space: str = "default",
    session: str = "litellm",
) -> Callable[..., Any]:
    """Helper factory returning a drop-in replacement for litellm.completion."""
    wrapper = UniMemLiteLLM(root=root, token_budget=token_budget, space=space, session=session)

    def _completion(model: str, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        return wrapper.completion(model=model, messages=messages, **kwargs)

    return _completion
