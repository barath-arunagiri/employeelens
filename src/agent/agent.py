"""
Employeelens — the agent.

Uses Databricks Foundation Model APIs (free pay-per-token).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from src.config import (
    DATABRICKS_MODEL,
    DATABRICKS_MODELS,
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
- "Who is the Project Manager?" -> run_sql: SELECT * FROM employees WHERE position ILIKE '%project%manager%'
- "Who are the Team Leads?" -> run_sql: SELECT * FROM employees WHERE position ILIKE '%team%lead%'
- "Who is in the Data Team?" -> find_employees_by_team
- "What are all the positions?" -> run_sql: SELECT DISTINCT position FROM employees WHERE position IS NOT NULL
- "Who has designation Data Engineer?" -> run_sql: SELECT * FROM employees WHERE designation ILIKE '%data%engineer%'
- "How many Team Leads are there?" -> run_sql: SELECT COUNT(*) FROM employees WHERE position ILIKE '%team%lead%'

GROUNDING RULES
- Every fact must come from a tool result in this conversation.
- If a tool returns no rows, say so plainly. Never invent people, projects, skills or certifications.
- Report counts exactly as returned.

WHAT THIS DATASET DOES NOT CONTAIN
- There are no reliable department, joining-date, location or salary fields. Position, designation and team may be available for some employees; report them when present and say "not recorded" when absent. Do not substitute project or skill category for department.

COVERAGE
- Skills are recorded for {with_skills} of {employees} people. When you report that nobody has a skill, mention that it only covers those with a profile.

STYLE
- Lead with the direct answer immediately. No preamble or explanation of process.
- Do NOT describe what tools you used or what you checked — users don't need to know.
- When showing a list of people/items, just show the list. Do NOT add a summary sentence repeating what's already in the list.
- Keep responses ultra-concise: 1 sentence for simple facts, just the list for "who" questions, short table for data comparisons.
- Example: "Shyamala Vignesh (2020024) is the Project Manager." NOT "To find the Project Manager, I checked..."
- Example for lists: Just show "• Person A\n• Person B\n• Person C" NOT "Here are the people: [list]. They work on X projects."
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


@dataclass
class AgentResult:
    answer: str
    trace: list[dict] = field(default_factory=list)
    rounds: int = 0
    backend: str = ""
    proposal: dict | None = None
    data: list[dict] = field(default_factory=list)
    proposals: list[dict] = field(default_factory=list)


def _extract_content_text(content) -> str:
    """
    Extract plain text from an OpenAI message ``content`` field.

    The Databricks Foundation Model ``databricks-gpt-oss-120b`` is a
    reasoning model that returns ``content`` as a list of content blocks
    (e.g. ``[{"type": "reasoning", ...}, {"type": "text", "text": "..."}]``)
    rather than a plain string.  This helper normalises both the classic
    string format and the structured-blocks format so the rest of the
    agent can always treat the answer as ``str``.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content)


def _openai_tools(tools: list[dict] = TOOLS) -> list[dict]:
    """
    Convert the shared tool schema into OpenAI function format.
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
    def __init__(self) -> None:
        self.backend = "databricks"
        self.model = DATABRICKS_MODEL
        self.models = DATABRICKS_MODELS  # Multi-model fallback list
        self.last_successful_model = None  # Track which model worked last

        if not self.model:
            raise RuntimeError(
                "DATABRICKS_MODEL is not set. Add DATABRICKS_MODEL=<endpoint> "
                "to your environment (e.g. DATABRICKS_MODEL=databricks-gpt-oss-120b)."
            )

        # databricks-openai provides an OpenAI-compatible client with
        # automatic Databricks authentication (OAuth M2M via the app's
        # service principal — no API key needed in Databricks Apps).
        #
        # max_retries=0 prevents the SDK from repeatedly retrying
        # HTTP 429 responses, which would make the app appear to hang.
        from databricks_openai import DatabricksOpenAI

        self.client = DatabricksOpenAI(max_retries=0)

    def _chat_with_retry(self, **kwargs):
        """
        Call models with automatic fallback on rate-limit (HTTP 429).

        Tries each model in DATABRICKS_MODELS list in sequence.
        If one model hits rate limit, immediately switches to the next.
        This prevents agent failures when a single model is rate-limited.
        """
        import time
        
        # Extract model from kwargs or use default
        requested_model = kwargs.get("model", self.model)
        
        # Build fallback sequence: start with requested model, then try others
        fallback_sequence = [requested_model]
        for model in self.models:
            if model != requested_model:
                fallback_sequence.append(model)
        
        last_error = None
        
        for attempt_no, model_to_try in enumerate(fallback_sequence, 1):
            try:
                # Update model in kwargs
                kwargs["model"] = model_to_try
                
                response = self.client.chat.completions.create(**kwargs)
                
                # Success! Track this model for logging
                self.last_successful_model = model_to_try
                
                return response
                
            except Exception as exc:
                last_error = exc
                
                # If it's a rate limit error and we have more models to try, continue
                if self._is_rate_limit_error(exc) and attempt_no < len(fallback_sequence):
                    time.sleep(0.5)  # Brief pause before trying next model
                    continue
                
                # If not a rate limit error, or no more models to try, raise
                if not self._is_rate_limit_error(exc) or attempt_no >= len(fallback_sequence):
                    raise
        
        # All models failed with rate limits
        if last_error:
            raise last_error

    def _get_model_display(self) -> str:
        """
        Get the display name for the model actually used.
        
        Returns the last successful model if available (showing which
        fallback model was used), otherwise the default model.
        """
        return self.last_successful_model or self.model

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
                    "\n\nNAME RESOLUTION:\n"
                    "- When given employee NAMES (not IDs), ALWAYS call search_employees FIRST to get their employee_id. "
                    "- If search_employees returns multiple matches for a name, list them and ask user to clarify. "
                    "- If no matches found, say so clearly - do not proceed with placeholder IDs. "
                    "- For batch requests (multiple people), search for each person individually. "
                    "\n\nPROPOSAL RULES:\n"
                    "- For a change request, use exactly one proposal tool PER EMPLOYEE. "
                    "- For batch requests (e.g., 'add cert to Barath, Nisha, Sanjai'), create MULTIPLE proposals. "
                    "- Proposal tools never write to DuckDB - they only create proposals. "
                    "\n\nHISTORY QUERIES:\n"
                    "- For history questions, use get_audit_history and answer from its rows without confirmation. "
                    "\n\nSTRICT RULES:\n"
                    "- Never invent employee IDs, records, or values. "
                    "- Always validate employee_id exists before creating proposal. "
                    "- If name resolution fails, explain why and ask for clarification."
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
                if tool["name"] in ["get_audit_history", "search_employees", "get_employee_360"]
            ]
        )

        for round_no in range(
            1,
            MAX_TOOL_ROUNDS + 1,
        ):
            try:
                response = self._chat_with_retry(
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
                            "All models reached rate limit (tried 4 fallback models). "
                            "Please wait a moment and try again."
                        ),
                        trace=trace,
                        rounds=round_no,
                        backend=f"Databricks · {self._get_model_display()}",
                    )

                return AgentResult(
                    answer=f"Databricks request failed: {exc}",
                    trace=trace,
                    rounds=round_no,
                    backend=f"Databricks · {self._get_model_display()}",
                )

            message = response.choices[0].message

            if not message.tool_calls:
                return AgentResult(
                    _extract_content_text(message.content).strip(),
                    trace,
                    round_no,
                    f"Databricks · {self.model}",
                )

            # Preserve assistant tool-call message for multi-round conversations
            messages.append(
                {
                    "role": "assistant",
                    "content": _extract_content_text(message.content),
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

            proposals = []

            for call in message.tool_calls:
                try:
                    args = json.loads(
                        call.function.arguments or "{}"
                    )

                except json.JSONDecodeError:
                    args = {}

                # Route read-only tools (search, audit) vs management proposal tools
                if call.function.name in ["get_audit_history", "search_employees", "get_employee_360"]:
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

                # Append tool result to messages so agent can use it in next round
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

                if "error" in output:
                    return AgentResult(
                        output["error"],
                        trace,
                        round_no,
                        f"Databricks · {self.model}",
                    )

                # Only management tools generate proposals, not read-only tools
                if call.function.name not in ["get_audit_history", "search_employees", "get_employee_360"]:
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
                    f"Databricks · {self.model}",
                    proposals[0],
                    [],
                    proposals,
                )

            # No proposals yet - continue loop if agent only called read-only tools
            # (search_employees, get_audit_history, etc.) to gather information

        return AgentResult(
            "I could not create a management proposal.",
            trace,
            MAX_TOOL_ROUNDS,
            f"Databricks · {self.model}",
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
                response = self._chat_with_retry(
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
                            "All models reached rate limit (tried 4 fallback models). "
                            "Please wait a moment and try again."
                        ),
                        trace=trace,
                        rounds=round_no,
                        backend=f"Databricks · {self._get_model_display()}",
                    )

                return AgentResult(
                    answer=f"Databricks request failed: {exc}",
                    trace=trace,
                    rounds=round_no,
                    backend=f"Databricks · {self._get_model_display()}",
                )

            message = response.choices[0].message

            # -----------------------------------------------------
            # Final natural-language answer
            # -----------------------------------------------------

            if not message.tool_calls:
                return AgentResult(
                    _extract_content_text(message.content).strip(),
                    trace,
                    round_no,
                    f"Databricks · {self.model}",
                )

            # -----------------------------------------------------
            # Preserve assistant tool-call message
            # -----------------------------------------------------

            messages.append(
                {
                    "role": "assistant",
                    "content": _extract_content_text(message.content),
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
            f"Databricks · {self.model}",
        )