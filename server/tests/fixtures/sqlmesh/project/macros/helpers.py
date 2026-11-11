from sqlmesh import macro


@macro()
def agg(evaluator, col):
    return f"SUM({col})"
