import ast
import astor
import inspect
from typing import Callable, Dict, List, Union

import jax

from .utils import read_object_name


def compile_to_sampler(model: Callable, namespace: Dict) -> Callable:
    """Compile the model in a function that generates prior predictive samples.

    Args:
        model: A probabilistic program definition.
        namespace: The model definition's global scope.

    Returns:
        sample_fn: A JIT compiled function that returns prior predictive
            samples from the model. The function's signature is of the form:

            `model_sampler(rng_key, *args, sample_shape=())`
    """
    source = inspect.getsource(model)
    tree = ast.parse(source)

    compiler = SamplerCompiler()
    tree = compiler.visit(tree)

    sampler_fn = compile(tree, filename="<ast>", mode="exec")
    exec(sampler_fn, namespace)  # execute the function in the model definition's scope
    sampler_fn = namespace[compiler.fn_name]
    sampler_fn = jax.jit(sampler_fn)
    return sampler_fn


class SamplerCompiler(ast.NodeTransformer):
    def __init__(self) -> None:
        super(SamplerCompiler, self).__init__()
        self.model_vars: List[str] = []
        self.fn_name: str = ""

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.FunctionDef:
        new_node = node
        new_node.args.args.insert(0, ast.arg(arg="rng_key", annotation=None, type_comment=None))
        new_node.args.args.append(ast.arg(arg="sample_shape", annotation=None, type_comment=None))
        new_node.args.defaults.append(ast.Tuple(elts=[], ctx=ast.Load()))
        new_node.decorator_list = []
        new_node.name = node.name + "_sampler"

        self.fn_name = new_node.name

        # We recursively visit nodes *after* having changed the function's
        # signature. We subsequently return the node once the children have
        # been modified.
        self.generic_visit(node)
        ast.copy_location(new_node, node)
        ast.fix_missing_locations(new_node)
        return new_node

    def visit_Expr(self, node: ast.Expr) -> Union[ast.Expr, ast.Assign]:
        """Visit the ast.Expr nodes.

        If the expression is a random variable assignement `x @ ...` we change the
        expression to bind the variable to the result of the corresponding sampling
        function. Otherwise we return the original node.

        Args:
            node:
                An `ast.Expr` node.

        Returns:
            A sampling assignment if the `ast.Expr` node correponds to a random variable assignment,
            the original node otherwise.

        Raises:
            ValueError:
                If the left-hand-side of the `@` symbol is not a variable name.
            ValueError:
                If the right-hand-side of the `@` symbol is not a function call.
        """
        if isinstance(node.value, ast.BinOp):
            if isinstance(node.value.op, ast.MatMult):

                # Check that we have a variable name on the let-hand side of `@`
                if isinstance(node.value.left, ast.Name):
                    var_name = node.value.left.id
                    self.model_vars.append(var_name)
                else:
                    raise ValueError(
                        "Expected a name on the left of the random variable assignement, got {}",
                        astor.code_gen.to_source(node.value.left),
                    )

                # Check that the right-hand side of `@` is a distribution initialization. if so
                # transform into a sampling expresion.
                # Note that we only add the `sample_shape` keyword argument to the leaf nodes of
                # the probabilistic graph.
                #
                # For this reason, and many others, I think it would be better to parse the model
                # definition into a probabilistic DAG that we can then compile with graph traversals.
                if isinstance(node.value.right, ast.Call):
                    distribution = read_object_name(node.value.right.func)
                    arguments = node.value.right.args
                    for arg in arguments:
                        if not (isinstance(arg, ast.Name) or isinstance(arg, ast.Constant)):
                            raise ValueError(
                                "Expected a random variable of a constant to initialize {}'s distribution, got {} instead.\n"
                                "Maybe you are trying to initialize a distribution directly, or call a function inside the "
                                "distribution initialization. While this would be a perfectly legitimate move, it is currently "
                                "not supported in mcmx. Use an intermediate variable instead: \n\n"
                                "Do not do `x @ Normal(Normal(0, 1), 1)` or `x @ Normal(my_function(10), 1)`, instead do "
                                " `y @ Normal(0, 1) & x @ Normal(y, 1)` and `y = my_function(10) & x @ Normal(y, 1)`".format(
                                    var_name, astor.code_gen.to_source(arg)
                                )
                            )
                    do_add_sample_shape = is_leaf_node(arguments, self.model_vars)
                    new_node = new_sample_expression(
                        var_name, distribution, arguments, do_add_sample_shape
                    )
                else:
                    raise ValueError(
                        "Expected a distribution initialization on the right of the random variable assignement, got {}",
                        astor.code_gen.to_source(node.value.right),
                    )

                ast.copy_location(new_node, node)
                ast.fix_missing_locations(new_node)
                return new_node

        return node


def is_leaf_node(
    params: List[Union[ast.expr, ast.Name, ast.Constant]], model_vars: List[str]
) -> bool:
    """Checks whether the current node is a leaf node. we define a leaf node as a random
    variable whose sample or logpdf value does not depend on another random variable.

    Concretely, since the graphical model is necessarily written in topological
    order (TODO: check this is true while parsing), we accumulate the names of
    the visited variables and compare the argument of the current variable's
    distribution initialization to this list. If there is a match, the current
    node is not a leaf node.

    Args:
        params: The parameters used to initialized the current random variable's distribution.
        model_vars: The random variables that have been visited so far in the traversal.

    Returns:
        A boolean that indicates whether the current random variable is a leaf node.
    """
    for p in params:
        if isinstance(p, ast.Name):
            if p.id in model_vars:
                return False
    return True


def new_sample_expression(
    name: str,
    distribution: str,
    arguments: List[Union[ast.expr, ast.Name, ast.Constant]],
    do_add_sample_shape: bool,
) -> ast.Assign:
    """Transforms a Random Variable definition into a sample expression.

    Example:

        If the random variable is a leaf node:

        >>> x @ Normal(0, 1)

        returns

        >>> x = Normal(0, 1).sample(rng_key, sample_shape)

        Otherwise

        >>> x @ Normal(0, 1)
        ... y @ Normal(x, 1)

        returns

        >>> x = Normal(0, 1).sample(rng_key, sample_shape)
        ... y = Norma(x, 1).sample(rng_key)

    Args:
        name: The name of the random variable being defined.
        distribution: The full path to the distribution object.
        arguments: Arguments used to initialize the distibution.
        do_add_sample_shape: whether to add the `sample_shape` keyword argument.

    Returns:
        An expression that assigns samples of the distribution to the variable.
    """
    args = [ast.Name(id="rng_key", ctx=ast.Load())]
    if do_add_sample_shape:
        args += (ast.Name(id="sample_shape", ctx=ast.Load()),)

    new_node = ast.Assign(
        targets=[ast.Name(id=name, ctx=ast.Store())],
        value=ast.Call(
            func=ast.Attribute(
                value=ast.Call(
                    func=ast.Name(id=distribution, ctx=ast.Load()), args=arguments, keywords=[],
                ),
                attr="sample",
                ctx=ast.Load(),
            ),
            args=args,
            keywords=[],
        ),
        type_comment=None,
    )
    return new_node