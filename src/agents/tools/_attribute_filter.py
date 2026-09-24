import ast
import operator
from functools import reduce

MAXIMUM_FILTER_CHARACTERS = 4000
SUPPORTED_FILTERS = (
    "filter_query takes comparisons of a column with a string, a number, True, "
    "False or None, using ==, !=, <, <=, >, >=, or in and not in with a list of "
    "those, joined with and, or, not and parentheses"
)
ORDERING_OPERATORS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
CONSTANT_TYPES = (str, int, float, bool, type(None))
SIGN_OPERATORS = {ast.USub: operator.neg, ast.UAdd: operator.pos}


class FilterRefused(ValueError):
    pass


def refused(reason: str) -> FilterRefused:
    return FilterRefused(f"{SUPPORTED_FILTERS}. {reason}")


def shown(node: ast.AST) -> str:
    return f"'{ast.unparse(node)}'"


def attribute_filter_mask(frame, expression: str):
    if len(expression) > MAXIMUM_FILTER_CHARACTERS:
        raise refused(f"This one is longer than {MAXIMUM_FILTER_CHARACTERS} characters.")
    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        raise refused(f"'{expression}' is not written that way.") from None
    try:
        return mask(frame, tree.body)
    except RecursionError:
        raise refused("This one is nested too deeply.") from None


def mask(frame, node: ast.AST):
    if isinstance(node, ast.BoolOp):
        combine = operator.and_ if isinstance(node.op, ast.And) else operator.or_
        return reduce(combine, (mask(frame, value) for value in node.values))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return ~mask(frame, node.operand)
    if isinstance(node, ast.Compare):
        operands = [node.left, *node.comparators]
        return reduce(
            operator.and_,
            (
                comparison(frame, left, comparator, right)
                for left, comparator, right in zip(operands, node.ops, operands[1:])
            ),
        )
    raise refused(f"{shown(node)} is not a comparison.")


def comparison(frame, left: ast.AST, comparator: ast.cmpop, right: ast.AST):
    if isinstance(comparator, (ast.In, ast.NotIn)):
        inside = column(frame, left).isin(constant_list(right))
        return ~inside if isinstance(comparator, ast.NotIn) else inside

    written = shown(ast.Compare(left=left, ops=[comparator], comparators=[right]))
    compare = ORDERING_OPERATORS.get(type(comparator))
    if compare is None:
        raise refused(f"{written} uses an operator outside that list.")
    if not (isinstance(left, ast.Name) or isinstance(right, ast.Name)):
        raise refused(f"{written} has no column on either side.")

    left_value, right_value = operand(frame, left), operand(frame, right)
    if is_none(left) or is_none(right):
        compared = left_value if isinstance(left, ast.Name) else right_value
        if isinstance(comparator, ast.Eq):
            return compared.isna()
        if isinstance(comparator, ast.NotEq):
            return compared.notna()
        raise refused("None can only be compared with == or !=.")
    return compare(left_value, right_value)


def operand(frame, node: ast.AST):
    if isinstance(node, ast.Name):
        return column(frame, node)
    return constant(node)


def column(frame, node: ast.AST):
    if not isinstance(node, ast.Name):
        raise refused(f"{shown(node)} is not a column name.")
    if node.id not in frame.columns:
        raise refused(f"'{node.id}' is not a column of this layer.")
    return frame[node.id]


def constant(node: ast.AST):
    if isinstance(node, ast.UnaryOp) and type(node.op) in SIGN_OPERATORS:
        value = constant(node.operand)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise refused(f"{shown(node)} puts a sign on something that is not a number.")
        return SIGN_OPERATORS[type(node.op)](value)
    if isinstance(node, ast.Constant) and isinstance(node.value, CONSTANT_TYPES):
        return node.value
    raise refused(f"{shown(node)} is not a column or a constant.")


def constant_list(node: ast.AST) -> list:
    if not isinstance(node, (ast.List, ast.Tuple)):
        raise refused(f"in and not in take a list of constants, not {shown(node)}.")
    return [constant(element) for element in node.elts]


def is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None
