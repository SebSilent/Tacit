"""Every prompt Tacit hardcodes, gathered in one place so it can be read.

The standing prompt's cost has always been on the dashboard; the rest of the
hardcoded text lived only in source. This is the inventory: tool descriptions,
the per-turn guidance blocks, the delivery orders, the Assistant persona, the
compaction, plan, research, learning and memory prompts.

It is not a second copy of anything. Every row is read from the constant the
sending code uses, so the panel cannot drift from the prompt. A row whose module
cannot be imported is skipped rather than guessed at.

Rows carry the text, where it came from, and what it measures. Nothing else:
the panel shows a title and the prompt.
"""

from __future__ import annotations

from . import tokens


def _row(name: str, text, source: str) -> dict | None:
    """One block of hardcoded text, measured. Empty text is not an entry."""
    if text is None:
        return None
    body = str(text)
    if not body.strip():
        return None
    return {"name": name, "text": body, "source": source,
            "chars": len(body), "tokens": tokens.estimate_tokens(body)}


def _group(gid: str, label: str, rows: list) -> dict | None:
    items = [r for r in rows if r]
    if not items:
        return None
    return {"id": gid, "label": label, "items": items,
            "chars": sum(i["chars"] for i in items),
            "tokens": sum(i["tokens"] for i in items)}


def catalog(project: str | None = None, sid: str = "") -> dict:
    """The whole inventory: grouped constants, plus the prompt as it reads now."""
    groups: list = []

    # ── the standing prompt ────────────────────────────────────────────────
    try:
        from .ai import prompts
        rows = [
            _row("BASE", prompts.BASE, "ai/prompts.py:BASE"),
            _row("VCS rule, off", prompts._VCS_OFF, "ai/prompts.py:_VCS_OFF"),
            _row("VCS rule, on", prompts._VCS_ON, "ai/prompts.py:_VCS_ON"),
            _row("PROJECT", prompts.PROJECT, "ai/prompts.py:PROJECT"),
            _row("WORKSPACE", prompts.WORKSPACE, "ai/prompts.py:WORKSPACE"),
            _row("READONLY", prompts.READONLY, "ai/prompts.py:READONLY"),
            _row("CHAT", prompts.CHAT, "ai/prompts.py:CHAT"),
            _row("SUBAGENT", prompts.SUBAGENT, "ai/prompts.py:SUBAGENT"),
            _row("ELIDED", prompts.ELIDED, "ai/prompts.py:ELIDED"),
        ]
        groups.append(_group("standing", "Standing prompt", rows))
    except Exception:  # noqa: BLE001
        pass

    # ── tool descriptions ──────────────────────────────────────────────────
    try:
        from . import agent
        rows = [_row(t["function"]["name"], t["function"]["description"], "agent.py:TOOLS")
                for t in agent.TOOLS]
        groups.append(_group("tools", "Tools", rows))
    except Exception:  # noqa: BLE001
        pass

    try:
        from . import plugin_manager
        rows = []
        for meta in plugin_manager.list_plugins():
            pid = meta.get("id") or ""
            for t in plugin_manager.tool_schemas(pid):
                f = t.get("function") or {}
                rows.append(_row(f.get("name") or pid, f.get("description") or "",
                                 f"plugins/{pid}.py"))
        groups.append(_group("plugin-tools", "Plugin tools", rows))
    except Exception:  # noqa: BLE001
        pass

    # ── what each profile puts in front of the model ───────────────────────
    try:
        from . import profiles
        lines = []
        for p in profiles.list_profiles():
            tools = p.get("tools")
            lines.append(
                f"{p['label']}  ({p['name']}){'  · active' if p['active'] else ''}\n"
                f"  {p['tool_count']} tools · ~{p['cost_display']} tokens\n"
                f"  tools: {'all' if tools is None else ', '.join(tools)}\n"
                f"  memory budget {p['memory_budget']}, plugins "
                f"{', '.join(p['plugins']) or 'none'}, MCP direct "
                f"{'on' if p['mcp_direct'] else 'off'}")
        groups.append(_group("profiles", "Profiles",
                             [_row("Profiles", "\n\n".join(lines), "profiles.py:BUILTIN")]))
    except Exception:  # noqa: BLE001
        pass

    # ── skills and knowledge ───────────────────────────────────────────────
    try:
        from . import skills
        rows = [_row("Skills index", skills.index(), "skills.py:index"),
                _row("Index preamble", skills.INDEX_PREAMBLE, "skills.py:INDEX_PREAMBLE")]
        for s in skills.load():
            rows.append(_row(f"{s['kind']}: {s['name']}", s["body"], s["path"]))
        groups.append(_group("skills", "Skills", rows))
    except Exception:  # noqa: BLE001
        pass

    # ── per-turn guidance ──────────────────────────────────────────────────
    try:
        from . import guidance
        rows = [_row(f"{name} failure", text, "guidance.py:CARDS")
                for name, text in sorted(guidance.CARDS.items())]
        rows += [
            _row("Discover", guidance._DISCOVER, "guidance.py:_DISCOVER"),
            _row("Repeat", guidance._REPEAT, "guidance.py:_REPEAT"),
            _row("Nothing written yet", guidance._ADVISORY, "guidance.py:_ADVISORY"),
            _row("Step budget", guidance._BUDGET, "guidance.py:_BUDGET"),
            _row("Failure", guidance._FAILED, "guidance.py:_FAILED"),
            _row("Timeout", guidance._TIMEOUT, "guidance.py:_TIMEOUT"),
            _row("Do it", guidance._DO_IT, "guidance.py:_DO_IT"),
            _row("Closing", guidance._CLOSING, "guidance.py:_CLOSING"),
            _row("Salvage, one file", guidance._SALVAGE_ONE, "guidance.py:_SALVAGE_ONE"),
            _row("Salvage, many files", guidance._SALVAGE_MANY, "guidance.py:_SALVAGE_MANY"),
            _row("Salvage, no name", guidance._SALVAGE_ANY, "guidance.py:_SALVAGE_ANY"),
            _row("Verify", guidance._VERIFY, "guidance.py:_VERIFY"),
            _row("Advice", guidance._ADVICE, "guidance.py:_ADVICE"),
            _row("Advice, late", guidance._ADVICE_LATE, "guidance.py:_ADVICE_LATE"),
            _row("Wall clock", guidance._TIME, "guidance.py:_TIME"),
            _row("Shell overlap",
                 "\n".join(f"{verb} → {tool}: {why}"
                           for verb, (tool, why) in sorted(guidance.SHELL_OVERLAP.items())),
                 "guidance.py:SHELL_OVERLAP"),
        ]
        groups.append(_group("guidance", "Guidance", rows))
    except Exception:  # noqa: BLE001
        pass

    # ── the delivery guarantee ─────────────────────────────────────────────
    try:
        from . import agent
        groups.append(_group("delivery", "Delivery", [
            _row("Repeat refused", agent._REPEAT_REPLY, "agent.py:_REPEAT_REPLY"),
            _row("Nothing written, in reply", agent._NOTHING_WRITTEN_REPLY,
                 "agent.py:_NOTHING_WRITTEN_REPLY"),
            _row("Severed call", agent.SEVERED, "agent.py:SEVERED"),
            _row("Pruned output", agent._PRUNED, "agent.py:_PRUNED"),
        ]))
    except Exception:  # noqa: BLE001
        pass

    # ── compaction ─────────────────────────────────────────────────────────
    try:
        from .ai import prompts
        groups.append(_group("compaction", "Compaction", [
            _row("COMPACT", prompts.COMPACT, "ai/prompts.py:COMPACT"),
            _row("SUMMARISED", prompts.SUMMARISED, "ai/prompts.py:SUMMARISED"),
        ]))
    except Exception:  # noqa: BLE001
        pass

    # ── the Assistant ──────────────────────────────────────────────────────
    try:
        from . import assistant
        groups.append(_group("assistant", "Assistant", [
            _row("PERSONA", assistant.PERSONA, "assistant.py:PERSONA"),
            _row("CAPABILITY_OFF", assistant.CAPABILITY_OFF, "assistant.py:CAPABILITY_OFF"),
            _row("CAPABILITY_ON", assistant.CAPABILITY_ON, "assistant.py:CAPABILITY_ON"),
            _row("CHAT_TASK", assistant.CHAT_TASK, "assistant.py:CHAT_TASK"),
            _row("CYCLE_PROMPT", assistant.CYCLE_PROMPT, "assistant.py:CYCLE_PROMPT"),
            _row("CONTINUE_PROMPT", assistant.CONTINUE_PROMPT, "assistant.py:CONTINUE_PROMPT"),
            _row("AUTONOMY_PERSONA", assistant.AUTONOMY_PERSONA,
                 "assistant.py:AUTONOMY_PERSONA"),
        ]))
    except Exception:  # noqa: BLE001
        pass

    # ── plan mode ──────────────────────────────────────────────────────────
    try:
        from . import plan
        groups.append(_group("plan", "Plan mode", [
            _row("DECOMPOSE", plan.DECOMPOSE, "plan.py:DECOMPOSE"),
            _row("QUESTIONS", plan.QUESTIONS, "plan.py:QUESTIONS"),
            _row("SYNTHESISE", plan.SYNTHESISE, "plan.py:SYNTHESISE"),
        ]))
    except Exception:  # noqa: BLE001
        pass

    # ── research ───────────────────────────────────────────────────────────
    try:
        from . import research
        groups.append(_group("research", "Research", [
            _row("DECOMPOSE", research.DECOMPOSE, "research.py:DECOMPOSE"),
            _row("SYNTHESISE", research.SYNTHESISE, "research.py:SYNTHESISE"),
            _row("Aspect brief",
                 "Research your aspect using fetch and the browser tools. Cite each source URL. "
                 "Report what you found, and say plainly what you could not confirm.",
                 "research.py:_aspect"),
        ]))
    except Exception:  # noqa: BLE001
        pass

    # ── learning ───────────────────────────────────────────────────────────
    try:
        from .ai import prompts
        groups.append(_group("learning", "Learning", [
            _row("PROPOSAL_SYSTEM", prompts.PROPOSAL_SYSTEM, "ai/prompts.py:PROPOSAL_SYSTEM"),
            _row("PROPOSAL_PASS", prompts.PROPOSAL_PASS, "ai/prompts.py:PROPOSAL_PASS"),
        ]))
    except Exception:  # noqa: BLE001
        pass

    # ── memory ─────────────────────────────────────────────────────────────
    try:
        from . import memory_modes
        rows = [_row("HEADER", memory_modes.HEADER, "memory_modes.py:HEADER")]
        try:
            from .routers import memory as memory_api
            rows += [
                _row("SUMMARY_PROMPT", memory_api._SUMMARY_PROMPT,
                     "routers/memory.py:_SUMMARY_PROMPT"),
                _row("COMPRESS_PROMPT", memory_api._COMPRESS_PROMPT,
                     "routers/memory.py:_COMPRESS_PROMPT"),
                _row("EXTRACT_PROMPT", memory_api._EXTRACT_PROMPT,
                     "routers/memory.py:_EXTRACT_PROMPT"),
            ]
        except Exception:  # noqa: BLE001
            pass
        groups.append(_group("memory", "Memory", rows))
    except Exception:  # noqa: BLE001
        pass

    # ── platform and probe text ────────────────────────────────────────────
    try:
        from . import sandbox, thinking
        groups.append(_group("policy", "Platform", [
            _row("SANDBOX_PROFILE", sandbox._SANDBOX_PROFILE, "sandbox.py:_SANDBOX_PROFILE"),
            _row("PROBE_QUESTION", thinking.PROBE_QUESTION, "thinking.py:PROBE_QUESTION"),
        ]))
    except Exception:  # noqa: BLE001
        pass

    # A group whose module would not import is empty, and an empty group says
    # nothing: it is dropped rather than shown as a heading with no rows.
    groups = [g for g in groups if g]
    flat = [i for g in groups for i in g["items"]]
    return {"ok": True, "groups": groups,
            "totals": {"entries": len(flat),
                       "chars": sum(i["chars"] for i in flat),
                       "tokens": sum(i["tokens"] for i in flat)},
            "exact": tokens.exact(),
            "live": _live(project, sid)}


def _live(project: str | None, sid: str = "") -> dict:
    """The prompt as this configuration renders it right now, not a template."""
    out: dict = {"blocks": []}
    try:
        from .ai import prompts
        out["agent"] = prompts.system_prompt(project or None, False, chat=False,
                                             session=sid or "")
        out["subagent"] = prompts.system_prompt(project or None, False, subagent=True)
        out["readonly"] = prompts.system_prompt(project or None, True, chat=False)
        out["chat"] = prompts.system_prompt(project or None, False, chat=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import project_context
        b = project_context.block(project or None) or {}
        if b.get("text"):
            out["blocks"].append({"name": "Project instructions", "text": b["text"]})
    except Exception:  # noqa: BLE001
        pass
    try:
        from . import memory_modes
        if memory_modes.is_on():
            m = memory_modes.startup(project or "") or {}
            if m.get("text"):
                out["blocks"].append({"name": "Memory", "text": m["text"]})
    except Exception:  # noqa: BLE001
        pass
    return out
