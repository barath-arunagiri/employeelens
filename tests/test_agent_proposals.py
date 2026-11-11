from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.agent.agent import PeopleLensAgent


class AgentProposalTests(unittest.TestCase):
    def test_same_certification_proposals_for_different_employees_are_kept(self):
        employee_ids = [f"2020{index:03d}" for index in range(6)]
        args_by_call = [
            {"employee_id": employee_id, "certification": "Databricks"}
            for employee_id in employee_ids
        ]
        args_by_call.append(args_by_call[0].copy())
        tool_calls = [
            SimpleNamespace(
                id=str(index),
                function=SimpleNamespace(
                    name="propose_add_certification",
                    arguments=json.dumps(args),
                ),
            )
            for index, args in enumerate(args_by_call)
        ]

        def make_proposal(_name, args):
            return {
                "row_count": 1,
                "rows": [{
                    "operation": "INSERT",
                    "table": "employee_certifications",
                    "employee_id": args["employee_id"],
                    "field": "certification",
                    "old_value": None,
                    "new_value": args["certification"],
                    "requires_confirmation": True,
                }],
                "note": "proposal only",
            }

        agent = PeopleLensAgent.__new__(PeopleLensAgent)
        proposals = []
        with patch(
            "src.agent.agent.run_management_tool",
            side_effect=make_proposal,
        ):
            agent._execute_tool_calls(
                SimpleNamespace(tool_calls=tool_calls),
                messages=[],
                trace=[],
                proposals=proposals,
                calls_used=0,
            )

        self.assertEqual(
            {proposal["employee_id"] for proposal in proposals},
            set(employee_ids),
        )
        self.assertEqual(len(proposals), 6)


if __name__ == "__main__":
    unittest.main()
