"""
Employeelens — the agent.

Uses Groq's OpenAI-compatible API with the configured model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from src.config import (
    GROQ_API_KEY,
    GROQ_MODEL,
    MAX_TOOL_ROUNDS,
)
from src.agent.tools import (
    MANAGEMENT_TOOLS,
    TOOLS,
    run_management_tool,
    run_tool,
)
from src.database import repository as repo


SYSTEM_PROMPT_TEMPLATE = """You are Employeelens, the employee data assistant for DataPattern, a Databricks consulting firm. You answer questions about the team using the tools provided.

HOW TO WORK
- Answer whatever is asked. If no specific tool fits, use run_sql; its description contains the schema.
- When the user names a person, call search_employees first. If more than one person matches, list candidates and ask which one they mean.
- Use find_employees when several conditions must hold for the same person.
- Chain as many read tool calls as the question needs before answering.
- Every management request must use exactly one proposal tool. Never execute a write from the read flow.
- Follow-ups such as "what about their projects" refer to the previous person or list.

ROUTING EXAMPLES
- "Who is employee 2020135?" -> search_employees or get_employee_360 by ID
- "Who is <name>?" -> search_employees first
- "Who knows Python?" -> find_employees_by_skill
- "Who knows Python and SQL?" -> find_employees with both skills
- "Who works on <project>?" -> find_employees_by_project
- "Who has <X> certification?" -> find_employees_by_certification
- "Who reports to <name>?" -> search_employees for the manager, then get_direct_reports
- "How many employees know <skill>?" -> find_employees_by_skill, report row_count, never estimate
- "Show <id>'s complete profile" -> get_employee_360
- "Show changes made to <id>" -> get_audit_history
- "Which project has the most people?" -> headcount_by_project
- "Top skills / most common skill" -> top_skills
- "Who has no skills recorded?" -> employees_without_skills

GROUNDING RULES
- Every fact must come from a tool result in this conversation.
- If a tool returns no rows, say so plainly. Never invent people, projects, skills or certifications.
- Report counts exactly as returned.

WHAT THIS DATASET DOES NOT CONTAIN
- There are no reliable department, joining-date, location or salary fields. Position, designation and team may be available for some employees; report them when present and say "not recorded" when absent. Do not substitute project or skill category for department.

COVERAGE
- Skills are recorded for {with_skills} of {employees} people. When you report that nobody has a skill, mention that it only covers those with a profile.

STYLE
Plain prose, usually two to five sentences. Lead with the answer. Use a short markdown table for more than three rows. No preamble.
"""


def _build_system_prompt() -> str:
    """
    Fill the coverage line from the live database instead of a hardcoded
    number.

    If the database cannot be read, use dataset-agnostic wording instead
    of showing stale or fabricated statistics.
    """
    try:
        stats = repo.dataset_stats()

        return SYSTEM_PROMPT_TEMPLATE.format(
            with_skills=stats["with_skills"],
            employees=stats["employees"],
        )

    except Exception:  # noqa: BLE001
        return SYSTEM_PROMPT_TEMPLATE.format(
            with_skills="only some",
            employees="all",
        )


# Models that are currently expected to support this application's
# tool-calling workflow.
GROQ_MODEL_ALIASES = {
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3-32b",
}


# Deprecated/decommissioned Groq models.
DEPRECATED_GROQ_MODELS = {
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
}


@dataclass
class AgentResult:
    answer: str
    trace: list[dict] = field(default_factory=list)
    rounds: int = 0
    backend: str = ""
    proposal: dict | None = None
    data: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)


def _openai_tools(tools: list[dict] = TOOLS) -> list[dict]:
    """
    Convert the shared tool schema into OpenAI/Groq function format.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]


class PeopleLensAgent:
    def __init__(self, backend: str | None = None):
        if backend not in (None, "groq"):
            raise ValueError(
                "Employeelens supports the Groq backend only"
            )

        self.backend = "groq" if GROQ_API_KEY else ""

        if not self.backend:
            raise RuntimeError(
                "No Groq API key found. Put GROQ_API_KEY in your .env file."
            )

        # Groq exposes an OpenAI-compatible API.
        #
        # IMPORTANT:
        # max_retries=0 prevents the OpenAI SDK from repeatedly retrying
        # HTTP 429 responses. Without this, the application can appear
        # to hang when Groq rate-limits the request.
        from openai import OpenAI

        self.client = OpenAI(
            api_key=GROQ_API_KEY,
            base_url="https://api.groq.com/openai/v1",
            max_retries=0,
        )

        self.model = self._resolve_model()

    def _resolve_model(self) -> str:
        """
        Validate GROQ_MODEL and use it — nothing else.

        There is deliberately no silent model fallback.
        """

        configured = (GROQ_MODEL or "").strip()

        if not configured:
            raise RuntimeError(
                "GROQ_MODEL is not set. Add GROQ_MODEL=<model-id> to your .env "
                "(e.g. GROQ_MODEL=openai/gpt-oss-120b)."
            )

        if configured in DEPRECATED_GROQ_MODELS:
            raise RuntimeError(
                f"GROQ_MODEL='{configured}' is a deprecated/decommissioned "
                "Groq model and is no longer supported by this application. "
                "Set GROQ_MODEL to a current tool-calling model, e.g. one of: "
                f"{sorted(GROQ_MODEL_ALIASES)}."
            )

        try:
            available = {
                model.id
                for model in self.client.models.list().data
            }

        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Could not reach Groq to validate "
                f"GROQ_MODEL='{configured}': {exc}"
            ) from exc

        if configured not in available:
            raise RuntimeError(
                f"GROQ_MODEL='{configured}' is not available for this "
                "Groq API key. Check the key's model access in the Groq "
                "console, or set GROQ_MODEL to one of the models this "
                f"key can access: {sorted(available) or 'none listed'}."
            )

        return configured

    def ask(
        self,
        question: str,
        history: list[dict] | None = None,
    ) -> AgentResult:
        """
        Answer a normal employee-data question.
        """
        return self._run_groq(
            question,
            history or [],
        )

    def propose_management(self, request: str) -> AgentResult:
        """
        Handle employee-data management requests.

        Management tools only create proposals.
        They do not directly write to DuckDB.
        """

        messages = [
            {
                "role": "system",
                "content": (
                    "You are the Employeelens HR data management assistant. "
                    "For a change request, use exactly one proposal tool. "
                    "Proposal tools never write to DuckDB. "
                    "For history questions, use get_audit_history and answer "
                    "from its rows without confirmation. "
                    "Never invent employee IDs, records, or values."
                ),
            },
            {
                "role": "user",
                "content": request,
            },
        ]

        trace: list[dict] = []

        management_tools = (
            MANAGEMENT_TOOLS
            + [
                tool
                for tool in TOOLS
                if tool["name"] == "get_audit_history"
            ]
        )

        for round_no in range(
            1,
            MAX_TOOL_ROUNDS + 1,
        ):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=800,
                    tools=_openai_tools(management_tools),
                    tool_choice="auto",
                    messages=messages,
                )

            except Exception as exc:  # noqa: BLE001
                # Handle Groq/OpenAI-compatible rate limiting without
                # allowing the SDK/application to hang.
                if self._is_rate_limit_error(exc):
                    return AgentResult(
                        answer=(
                            "Groq rate limit reached. "
                            "Please wait a moment and try again."
                        ),
                        trace=trace,
                        rounds=round_no,
                        backend=f"Groq · {self.model}",
                    )

                return AgentResult(
                    answer=f"Groq request failed: {exc}",
                    trace=trace,
                    rounds=round_no,
                    backend=f"Groq · {self.model}",
                )

            message = response.choices[0].message

            if not message.tool_calls:
                return AgentResult(
                    (message.content or "").strip(),
                    trace,
                    round_no,
                    f"Groq · {self.model}",
                )

            proposals = []

            for call in message.tool_calls:
                try:
                    args = json.loads(
                        call.function.arguments or "{}"
                    )

                except json.JSONDecodeError:
                    args = {}

                if call.function.name == "get_audit_history":
                    output = run_tool(
                        call.function.name,
                        args,
                    )
                else:
                    output = run_management_tool(
                        call.function.name,
                        args,
                    )

                trace.append(
                    {
                        "tool": call.function.name,
                        "input": args,
                        "row_count": output.get("row_count"),
                        "output": output,
                    }
                )

                if "error" in output:
                    return AgentResult(
                        output["error"],
                        trace,
                        round_no,
                        f"Groq · {self.model}",
                    )

                if call.function.name != "get_audit_history":
                    proposals.extend(
                        output.get("rows", [])
                    )

            if proposals:
                return AgentResult(
                    (
                        "Please review the proposed changes and "
                        "confirm them in the Data Management tab."
                    ),
                    trace,
                    round_no,
                    f"Groq · {self.model}",
                    proposals[0],
                    [],
                    proposals,
                )

            return AgentResult(
                "Audit history retrieved.",
                trace,
                round_no,
                f"Groq · {self.model}",
            )

        return AgentResult(
            "I could not create a management proposal.",
            trace,
            MAX_TOOL_ROUNDS,
            f"Groq · {self.model}",
        )

    # -------------------------------------------------------------
    # Error handling
    # -------------------------------------------------------------

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """
        Detect Groq/OpenAI-compatible HTTP 429 errors.

        Importing RateLimitError directly is avoided here so the
        application remains tolerant of different OpenAI SDK versions.
        """

        error_name = type(exc).__name__.lower()

        if "ratelimit" in error_name:
            return True

        status_code = getattr(exc, "status_code", None)

        if status_code == 429:
            return True

        response = getattr(exc, "response", None)

        if response is not None:
            response_status = getattr(
                response,
                "status_code",
                None,
            )

            if response_status == 429:
                return True

        return False

    # -------------------------------------------------------------
    # Groq
    # -------------------------------------------------------------

    def _run_groq(
        self,
        question: str,
        history: list[dict],
    ) -> AgentResult:

        messages = [
            {
                "role": "system",
                "content": _build_system_prompt(),
            }
        ]

        messages += list(history)

        messages.append(
            {
                "role": "user",
                "content": question,
            }
        )

        trace: list[dict] = []

        for round_no in range(
            1,
            MAX_TOOL_ROUNDS + 1,
        ):

            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=1500,
                    tools=_openai_tools(),
                    tool_choice="auto",
                    messages=messages,
                )

            except Exception as exc:  # noqa: BLE001
                # 429 / rate-limit handling.
                #
                # The important part is that this returns immediately
                # instead of allowing the SDK to perform repeated retries.
                if self._is_rate_limit_error(exc):
                    return AgentResult(
                        answer=(
                            "Groq rate limit reached. "
                            "Please wait a moment and try again."
                        ),
                        trace=trace,
                        rounds=round_no,
                        backend=f"Groq · {self.model}",
                    )

                return AgentResult(
                    answer=f"Groq request failed: {exc}",
                    trace=trace,
                    rounds=round_no,
                    backend=f"Groq · {self.model}",
                )

            message = response.choices[0].message

            # -----------------------------------------------------
            # Final natural-language answer
            # -----------------------------------------------------

            if not message.tool_calls:
                return AgentResult(
                    (message.content or "").strip(),
                    trace,
                    round_no,
                    f"Groq · {self.model}",
                )

            # -----------------------------------------------------
            # Preserve assistant tool-call message
            # -----------------------------------------------------

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in message.tool_calls
                    ],
                }
            )

            # -----------------------------------------------------
            # Execute requested read tools
            # -----------------------------------------------------

            for call in message.tool_calls:

                try:
                    args = json.loads(
                        call.function.arguments or "{}"
                    )

                except json.JSONDecodeError:
                    args = {}

                output = run_tool(
                    call.function.name,
                    args,
                )

                trace.append(
                    {
                        "tool": call.function.name,
                        "input": args,
                        "row_count": output.get("row_count"),
                        "output": output,
                    }
                )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.function.name,
                        "content": json.dumps(
                            output,
                            default=str,
                        )[:16000],
                    }
                )

        return AgentResult(
            (
                "I could not finish that within the tool-call limit. "
                "Try a narrower question."
            ),
            trace,
            MAX_TOOL_ROUNDS,
            f"Groq · {self.model}",
        )