"""
Efficient live routing check.

This test verifies that the configured Groq model selects the correct
FIRST application tool for each canonical employee-data question.

It does NOT execute the tools and does NOT judge the final prose answer.

Run:

    python -m tests.test_routing_live

Exit code:
    0 = all cases routed correctly
    1 = one or more cases failed
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parent.parent),
)

from src.agent.agent import (  # noqa: E402
    PeopleLensAgent,
    _openai_tools,
)


# ---------------------------------------------------------------------
# Canonical routing cases
# ---------------------------------------------------------------------

CASES: list[tuple[str, set[str]]] = [
    (
        "Who is employee 2020135?",
        {
            "get_employee_360",
            "search_employees",
        },
    ),
    (
        "Who is Seshwanth?",
        {
            "search_employees",
        },
    ),
    (
        "Who knows Python?",
        {
            "find_employees_by_skill",
            "find_employees",
        },
    ),
    (
        "Who knows Python and SQL?",
        {
            "find_employees",
            "find_employees_by_skill",
        },
    ),
    (
        "Who works on Project Phoenix?",
        {
            "find_employees_by_project",
        },
    ),
    (
        "Who has Databricks certification?",
        {
            "find_employees_by_certification",
        },
    ),
    (
        "Who reports to Uma?",
        {
            "search_employees",
            "get_direct_reports",
        },
    ),
    (
        "How many employees know Python?",
        {
            "find_employees_by_skill",
            "find_employees",
            "run_sql",
        },
    ),
    (
        "Show employee 2020135's complete profile.",
        {
            "get_employee_360",
        },
    ),
    (
        "Show all changes made to 2020135.",
        {
            "get_audit_history",
        },
    ),
    (
        "Which project has the most people?",
        {
            "headcount_by_project",
        },
    ),
    (
        "What are the top five skills in the organisation?",
        {
            "top_skills",
        },
    ),
    (
        "Whose profile is incomplete?",
        {
            "employees_without_skills",
        },
    ),
]


# ---------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------

TEST_DELAY_SECONDS = float(
    os.getenv(
        "GROQ_TEST_DELAY_SECONDS",
        "1",
    )
)

# GPT-OSS is a reasoning model.
# Low reasoning is enough for this simple routing task and leaves
# enough completion budget for the actual tool call.
ROUTING_REASONING_EFFORT = os.getenv(
    "GROQ_ROUTING_REASONING_EFFORT",
    "low",
)

ROUTING_MAX_COMPLETION_TOKENS = int(
    os.getenv(
        "GROQ_ROUTING_MAX_COMPLETION_TOKENS",
        "2048",
    )
)


# ---------------------------------------------------------------------
# Small routing-only system prompt
# ---------------------------------------------------------------------

ROUTING_SYSTEM_PROMPT = """
You are a tool router for an employee-data assistant.

Your ONLY job is to select the FIRST tool that should handle the user's
request.

You MUST call exactly one tool.

Do NOT answer the user's question.
Do NOT provide an explanation.
Do NOT return prose.

Routing rules:

1. Person lookup by employee ID or name:
   - search_employees
   - get_employee_360 may be used directly for a complete profile.

2. "Who knows <skill>?"
   - find_employees_by_skill

3. "Who knows <skill> and <skill>?"
   - find_employees

4. "Who works on <project>?"
   - find_employees_by_project

5. Certification questions:
   - find_employees_by_certification

6. Reporting relationship:
   - search_employees for the manager first
   - get_direct_reports is also acceptable when the manager is already known

7. Employee count by skill:
   - find_employees_by_skill
   - find_employees
   - run_sql

8. Complete employee profile:
   - get_employee_360

9. Audit/change history:
   - get_audit_history

10. Project with the most people:
    - headcount_by_project

11. Most common/top skills:
    - top_skills

12. Employees without recorded skills:
    - employees_without_skills

Use the tool names exactly as provided in the tool definitions.
"""


# ---------------------------------------------------------------------
# Rate-limit detection
# ---------------------------------------------------------------------

def is_rate_limit_error(exc: Exception) -> bool:
    error_name = type(exc).__name__.lower()

    if "ratelimit" in error_name:
        return True

    status_code = getattr(
        exc,
        "status_code",
        None,
    )

    if status_code == 429:
        return True

    response = getattr(
        exc,
        "response",
        None,
    )

    if response is not None:
        if getattr(
            response,
            "status_code",
            None,
        ) == 429:
            return True

    return False


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:

    agent = PeopleLensAgent()

    print(
        f"Routing check — {len(CASES)} cases against {agent.model}"
    )
    print(
        f"Reasoning effort: {ROUTING_REASONING_EFFORT}"
    )
    print(
        f"Max completion tokens: "
        f"{ROUTING_MAX_COMPLETION_TOKENS}"
    )
    print("-" * 60)

    failures = 0

    tools = _openai_tools()

    for index, (prompt, acceptable) in enumerate(
        CASES,
        start=1,
    ):

        if (
            index > 1
            and TEST_DELAY_SECONDS > 0
        ):
            time.sleep(
                TEST_DELAY_SECONDS
            )

        messages = [
            {
                "role": "system",
                "content": ROUTING_SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": prompt,
            },
        ]

        try:

            response = agent.client.chat.completions.create(
                model=agent.model,

                # GPT-OSS reasoning configuration.
                reasoning_effort=ROUTING_REASONING_EFFORT,

                # Groq recommends max_completion_tokens for reasoning
                # models.
                max_completion_tokens=ROUTING_MAX_COMPLETION_TOKENS,

                messages=messages,

                tools=tools,

                # The routing test requires a tool call.
                tool_choice="required",

                # We only need one first tool.
                parallel_tool_calls=False,

                # Do not return reasoning text for this test.
                include_reasoning=False,
            )

        except Exception as exc:

            if is_rate_limit_error(exc):

                print(
                    f"RATE_LIMITED  {prompt!r:<50}"
                    f" -> Groq HTTP 429"
                )

                failures += 1
                continue

            print(
                f"ERROR         {prompt!r:<50}"
                f" -> {type(exc).__name__}: {exc}"
            )

            failures += 1
            continue

        choice = response.choices[0]

        message = choice.message

        # -------------------------------------------------------------
        # The expected Groq tool-call response is:
        #
        # finish_reason = "tool_calls"
        # message.tool_calls = [...]
        #
        # -------------------------------------------------------------

        if not message.tool_calls:

            print(
                f"FAIL          {prompt!r:<55}"
                f" -> None"
            )

            print(
                f"              finish_reason="
                f"{choice.finish_reason}"
            )

            content = getattr(
                message,
                "content",
                None,
            )

            if content:
                print(
                    f"              content="
                    f"{content!r}"
                )

            reasoning = getattr(
                message,
                "reasoning",
                None,
            )

            if reasoning:
                print(
                    "              reasoning returned"
                )

            failures += 1
            continue

        # -------------------------------------------------------------
        # First tool
        # -------------------------------------------------------------

        first_call = message.tool_calls[0]

        first_tool = first_call.function.name

        # -------------------------------------------------------------
        # Arguments
        # -------------------------------------------------------------

        try:

            arguments = json.loads(
                first_call.function.arguments
                or "{}"
            )

        except json.JSONDecodeError:

            arguments = {}

        # -------------------------------------------------------------
        # Check
        # -------------------------------------------------------------

        ok = first_tool in acceptable

        if ok:

            print(
                f"ok    {prompt!r:<55}"
                f" -> {first_tool}"
                f"  (expected one of "
                f"{sorted(acceptable)})"
            )

        else:

            failures += 1

            print(
                f"FAIL  {prompt!r:<55}"
                f" -> {first_tool}"
                f"  (expected one of "
                f"{sorted(acceptable)})"
            )

            print(
                f"      arguments: {arguments}"
            )

    print("-" * 60)

    if failures == 0:

        print(
            "all routed correctly"
        )

        sys.exit(0)

    print(
        f"{failures} case(s) failed"
    )

    sys.exit(1)


if __name__ == "__main__":
    main()