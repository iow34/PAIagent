from dataclasses import dataclass, field

from app.query_types import QueryExecutionError

MAX_SPECIALIST_CALLS = 8
MAX_TOOL_CALLS = 20
MAX_MODEL_CALLS = 8


class QueryBudgetExceededError(QueryExecutionError):
    pass


@dataclass
class QueryExecutionBudget:
    specialist_calls: int = field(default=0, init=False)
    tool_calls: int = field(default=0, init=False)
    model_calls: int = field(default=0, init=False)

    def reserve(
        self,
        *,
        specialist_calls: int = 0,
        tool_calls: int = 0,
        model_calls: int = 0,
    ) -> None:
        if specialist_calls < 0 or tool_calls < 0 or model_calls < 0:
            raise ValueError("Reserved call counts must not be negative.")

        next_specialist_calls = self.specialist_calls + specialist_calls
        next_tool_calls = self.tool_calls + tool_calls
        next_model_calls = self.model_calls + model_calls

        if next_specialist_calls > MAX_SPECIALIST_CALLS:
            raise QueryBudgetExceededError("Query exceeded the specialist call budget.")

        if next_tool_calls > MAX_TOOL_CALLS:
            raise QueryBudgetExceededError("Query exceeded the tool call budget.")

        if next_model_calls > MAX_MODEL_CALLS:
            raise QueryBudgetExceededError("Query exceeded the model call budget.")

        self.specialist_calls = next_specialist_calls
        self.tool_calls = next_tool_calls
        self.model_calls = next_model_calls
