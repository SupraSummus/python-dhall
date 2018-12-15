from typing import Any, Optional
import attr

from .data_structures import ShadowDict


CTX_EMPTY = ShadowDict()
DEFAULT_VARIABLE_NAME = '_'


def unique(elements):
    return len(elements) == len(set(elements))


def exact(a, b):
    """a ≡ b"""
    return a.evaluated().normalized() == b.evaluated().normalized()


def function_check(arg, result):
    """arg ↝ result : return"""
    if result == TypeBuiltin():
        return TypeBuiltin()
    if arg == KindBuiltin() and result == KindBuiltin():
        return KindBuiltin()
    if arg == SortBuiltin() and result in (KindBuiltin(), SortBuiltin()):
        return SortBuiltin()
    raise TypeError('Function check failed for `{} ↝ {}`'.format(
        arg.to_dhall(),
        result.to_dhall(),
    ))


def increase_indent(s):
    return '\t' + s.replace('\n', '\n\t')


def ctx_empty(ctx):
    for k, vs in ctx.entries.items():
        for (typ, value, covered_ctx), _ in vs:
            if typ is not None or value is not None:
                return False
    return True


def ctx2str(ctx):
    parts = []
    for k, vs in sorted(ctx.entries.items()):
        for scope, ((typ, value, covered_ctx), _) in enumerate(vs):

            if typ is not None:
                s = '`{}@{}` has type `{}`'.format(
                    k, scope, typ.to_dhall(),
                )
            if value is not None:
                s = '`{}@{}` has value `{}`'.format(
                    k, scope, value.to_dhall(),
                )

            covered_ctx_str = ctx2str(covered_ctx)
            if covered_ctx_str:
                s += ' where \n' + increase_indent(covered_ctx_str)

            parts.append(s)

    return '\n'.join(parts)


@attr.s(frozen=True, auto_attribs=True)
class Expression:
    context: ShadowDict = CTX_EMPTY

    def normalized(self, ctx=CTX_EMPTY):
        """Perform alpha-normalization. ctx contains new names for variables."""
        return self._normalized(ctx)

    def _normalized(self, ctx):
        return self.map(lambda expr: expr.normalized(ctx))

    def evaluated(self):
        """self ⇥ return"""
        return self._evaluated()

    def _evaluated(self):
        context = self.context
        return attr.evolve(
            self,
            context=CTX_EMPTY,
        ).map(lambda expr: expr.substitute_many(context).evaluated())

    def type(self, type_ctx=CTX_EMPTY):
        """Type of this expression.
        type_ctx contains types for variables.
        value_ctx contains variable values to substitute
        """
        try:
            return self._type(type_ctx)
        except TypeError:
            raise TypeError(
                (
                    "when type-infering\n"
                    "\t`{}`\n"
                    "{}"
                ).format(
                    self.to_dhall(),
                    ctx2str(type_ctx),
                ),
            )

    def _type(self, type_ctx):
        raise NotImplementedError('{}._type() is not implemented'.format(self.__class__))

    def normalized_type(self, type_ctx=CTX_EMPTY):
        """self :⇥ return"""
        typ, typ_ctx = self.type(type_ctx)
        return typ.evaluated().normalized()

    def exact(self, other):
        """self ≡ other"""
        return exact(self, other)

    def can_apply_to(self, value):
        return False

    def apply(self, value):
        raise NotImplementedError('{}.apply() is not implemented'.format(self.__class__))

    def map(self, f):
        current_expression = self
        return attr.evolve(current_expression, **{
            k: f(v)
            for k, v in attr.asdict(current_expression, recurse=False).items()
            if isinstance(v, Expression)
        })

    def substitute_single(self, name, value):
        return attr.evolve(
            self,
            context=self.context.shadow_single(name, value),
        )

    def substitute_many(self, context):
        return attr.evolve(
            self,
            context=self.context.join(context),
        )

    def to_dhall(self):
        return str(self)  # TODO change to not implemented someday

    def to_python(self):
        """Representation in python's native data types."""
        raise NotImplementedError('{}.to_python() is not implemented yet'.format(self.__class__))


@attr.s(frozen=True, auto_attribs=True)
class Lambda(Expression):
    parameter_name: str
    parameter_type: Expression
    expression: Expression
    context: ShadowDict = CTX_EMPTY

    def _normalized(self, ctx):
        return Lambda(
            DEFAULT_VARIABLE_NAME,
            self.parameter_type.normalized(ctx),
            self.expression.normalized(ctx.shadow({
                self.parameter_name: DEFAULT_VARIABLE_NAME,
            })),
        )

    def _evaluated(self):
        return attr.evolve(
            self,
            parameter_type=self.parameter_type.substitute_many(self.context).evaluated(),
            expression=self.expression.substitute_many(self.context).substitute_single(
                self.parameter_name,
                None,
            ).evaluated(),
            context=CTX_EMPTY,
        )

    def _type(self, type_ctx):
        parameter_type = self.parameter_type.substitute_many(self.context)
        expression_type, expression_type_ctx = self.expression.substitute_many(self.context).type(type_ctx.shadow({
            self.parameter_name: (parameter_type, None, type_ctx),
        }))
        lambda_type = ForAll(
            self.parameter_name,
            parameter_type,
            expression_type,
        )
        lambda_type.type(type_ctx)  # lambda type must typecheck
        return lambda_type, type_ctx

    def can_apply_to(self, value):
        return True

    def apply(self, value):
        return self.expression.substitute_single(self.parameter_name, value).evaluated()

    def to_dhall(self):
        return 'λ({} : {}) → {}'.format(
            self.parameter_name,
            self.parameter_type.to_dhall(),
            self.expression.to_dhall(),
        )


@attr.s(frozen=True, auto_attribs=True)
class Conditional(Expression):
    condition: Expression
    if_true: Expression
    if_false: Expression
    context: ShadowDict = CTX_EMPTY

    def _normalized(self, ctx):
        return Conditional(
            self.condition.normalized(ctx),
            self.if_true.normalized(ctx),
            self.if_false.normalized(ctx),
        )


@attr.s(frozen=True, auto_attribs=True)
class LetIn(Expression):
    parameters: [(
        str,  # name
        Expression,  # value
        Optional[Expression],  # type
    )]
    expression: Expression
    context: ShadowDict = CTX_EMPTY

    def _normalized(self, ctx):
        normalized_parameters = []
        for name, value, typ in self.parameters:
            normalized_parameters.append((
                DEFAULT_VARIABLE_NAME,
                value.normalized(ctx),
                None if typ is None else typ.normalized(ctx),
            ))
            ctx = ctx.shadow({
                name: DEFAULT_VARIABLE_NAME,
            })
        return LetIn(
            normalized_parameters,
            self.expression.normalized(ctx),
        )

    def _evaluated(self):
        context = self.context
        for name, value, typ in self.parameters:
            context = context.shadow_single(
                name,
                value.substitute_many(context),
            )
        return self.expression.substitute_many(context).evaluated()

    def _type(self, type_ctx):
        context = self.context
        for name, value, typ in self.parameters:
            value = value.substitute_many(context)
            value_type, value_type_ctx = value.type(type_ctx)

            # verify against annotation
            if typ is not None:
                typ = typ.substitute_many(context)
                typ.type(type_ctx)  # type annotation typechecks itself
                if not exact(typ, value_type):
                    raise TypeError('annotation\n\t{} doesn\'t match expression type\n\t{}'.format(
                        typ.to_dhall(),
                        value_type.to_dhall(),
                    ))

            context = context.shadow_single(name, value)

        return self.expression.substitute_many(context).type(type_ctx)

    def to_dhall(self):
        lets = []
        for name, value, typ in self.parameters:
            if typ is None:
                lets.append('let {} = {}'.format(name, value.to_dhall()))
            else:
                lets.append('let {} = {} : {}'.format(name, value.to_dhall(), typ.to_dhall()))
        return '{} in {}'.format(' '.join(lets), self.expression.to_dhall())


@attr.s(frozen=True, auto_attribs=True)
class ForAll(Expression):
    parameter_name: str
    parameter_type: Expression
    expression: Expression
    context: ShadowDict = CTX_EMPTY

    def _normalized(self, ctx):
        return ForAll(
            DEFAULT_VARIABLE_NAME,
            self.parameter_type.normalized(ctx),
            self.expression.normalized(ctx.shadow({
                self.parameter_name: DEFAULT_VARIABLE_NAME,
            })),
        )

    def _evaluated(self):
        return attr.evolve(
            self,
            parameter_type=self.parameter_type.substitute_many(self.context).evaluated(),
            expression=self.expression.substitute_many(self.context).substitute_single(
                self.parameter_name,
                None,
            ).evaluated(),
            context=CTX_EMPTY,
        )

    def _type(self, type_ctx):
        parameter_type = self.parameter_type.substitute_many(self.context)
        parameter_type_type = parameter_type.normalized_type(type_ctx)
        expression_type = self.expression.substitute_many(self.context).normalized_type(type_ctx.shadow({
            self.parameter_name: (parameter_type, None, type_ctx),
        }))
        return function_check(parameter_type_type, expression_type), CTX_EMPTY

    def to_dhall(self):
        return '∀({} : {}) → {}'.format(
            self.parameter_name,
            self.parameter_type.to_dhall(),
            self.expression.to_dhall(),
        )


@attr.s(frozen=True, auto_attribs=True)
class Variable(Expression):
    name: str
    scope: int = 0
    context: ShadowDict = CTX_EMPTY

    def _normalized(self, ctx):
        if ctx.has(self.name, self.scope):
            # bound variable
            return Variable(
                ctx.get(self.name, self.scope),
                ctx.age(self.name, self.scope),
            )
        else:
            # free variable
            return self

    def _evaluated(self):
        if self.context.has(self.name, self.scope):
            value = self.context.get(self.name, self.scope)
            if value is not None:
                return value.evaluated()
        return self

    def _type(self, type_ctx):
        if self.context.has(self.name, self.scope):
            value = self.context.get(self.name, self.scope)
            if value is not None:
                return value.type(type_ctx)

        if type_ctx.has(self.name, self.scope):
            typ, value, covered_ctx = type_ctx.get(self.name, self.scope)
            if value is not None:
                return value.type(covered_ctx)
            if typ is not None:
                return typ, covered_ctx

        raise TypeError('unbound variable {}'.format(self))

    def __str__(self):
        if self.scope == 0:
            return self.name
        else:
            return '{}@{}'.format(self.name, self.scope)


@attr.s(frozen=True, auto_attribs=True)
class TypeAnnotation(Expression):
    expression: Expression
    expression_type: Expression
    context: ShadowDict = CTX_EMPTY

    def _evaluated(self):
        return self.expression.substitute_many(self.context).evaluated()

    def _type(self, type_ctx):
        annotated_type = self.expression_type.substitute_many(self.context)
        annotated_type.type(type_ctx)  # the type itself typechecks
        typ, typ_ctx = self.expression.substitute_many(self.context).type(type_ctx)
        if not exact(typ, annotated_type):
            raise TypeError('annotation\n\t`{}` doesn\'t match expression type\n\t`{}`'.format(
                self.expression_type.to_dhall(),
                typ.to_dhall(),
            ))
        return annotated_type, type_ctx

    def to_dhall(self):
        return '{} : {}'.format(self.expression.to_dhall(), self.expression_type.to_dhall())


@attr.s(frozen=True, auto_attribs=True)
class BinaryOperatorExpression(Expression):
    arg1: Expression
    arg2: Expression
    context: ShadowDict = CTX_EMPTY

    dhall_operator_string = None

    def to_dhall(self):
        return '({} {} {})'.format(self.arg1.to_dhall(), self.dhall_operator_string, self.arg2.to_dhall())


class ListAppendExpression(BinaryOperatorExpression):
    dhall_operator_string = '#'

    def _evaluated(self):
        new = super()._evaluated()
        if isinstance(new.arg1, ListLiteral) and isinstance(new.arg2, ListLiteral):
            return ListLiteral(new.arg1.items + new.arg2.items)
        else:
            return new


class ApplicationExpression(BinaryOperatorExpression):
    def _evaluated(self):
        new = super()._evaluated()
        f = new.arg1
        arg = new.arg2
        if f.can_apply_to(arg):
            return f.apply(arg)
        else:
            return new

    def _type(self, type_ctx):
        f = self.arg1.substitute_many(self.context)
        arg = self.arg2.substitute_many(self.context)
        function_type = f.normalized_type(type_ctx)
        if not isinstance(function_type, ForAll):
            raise TypeError('couldnt apply non-function `{}`'.format(arg.to_dhall()))
        parameter_type, parameter_type_ctx = arg.type(type_ctx)
        if not exact(parameter_type, function_type.parameter_type):
            raise TypeError('Function expects argument of type {}, but got {}.'.format(
                function_type.parameter_type,
                parameter_type,
            ))
        return function_type.expression, type_ctx.shadow({
            function_type.parameter_name: (None, self.arg2, parameter_type_ctx),
        })

    def to_dhall(self):
        return '({} {})'.format(self.arg1.to_dhall(), self.arg2.to_dhall())


@attr.s(frozen=True, auto_attribs=True)
class MergeExpression(Expression):
    handlers: Expression
    union: Expression
    result_type: Optional[Expression] = None
    context: ShadowDict = CTX_EMPTY

    def _type(self, type_ctx):
        handlers_type = self.handlers.normalized_type(type_ctx)
        if not isinstance(handlers_type, RecordType):
            raise TypeError("expected record as a first argument to `merge` but `{}` has type `{}`".format(
                self.handlers.to_dhall(), handlers_type.to_dhall(),
            ))

        union_type = self.union.normalized_type(type_ctx)
        if not isinstance(union_type, UnionType):
            raise TypeError("expected union as a second argument to `merge` but `{}` has type `{}`".format(
                self.union.to_dhall(), union_type.to_dhall(),
            ))

        handlers_type_fields = sorted(handlers_type.fields)
        union_type_alternatives = sorted(union_type.alternatives)

        if [f[0] for f in handlers_type_fields] != [f[0] for f in union_type_alternatives]:
            raise TypeError("union and handlers must have exactly same field names set")

        output_type = None
        output_type_ctx = None
        if self.result_type is not None:
            output_type = self.result_type.evaluated(type_ctx)
            output_type_ctx = type_ctx
        for (name, handler_type), (_, input_type) in zip(handlers_type_fields, union_type_alternatives):
            if not isinstance(handler_type, ForAll):
                raise TypeError("handler for field `{}` is not a function, but {}".format(
                    name,
                    handler_type.to_dhall(),
                ))
            if not exact(handler_type.parameter_type, input_type):
                raise TypeError("handler for field `{}` expects `{}` as input, but union contains `{}`".format(
                    name,
                    handler_type.parameter_type.format(),
                    input_type.format(),
                ))
            new_output_type = handler_type.expression
            new_output_type_ctx = type_ctx.shadow({
                handler_type.parameter_name: (None, None, None),  # require that this is not a free variable
            })
            if output_type is not None:
                if output_type.evaluated(output_type_ctx).normalized() != new_output_type.evaluated(new_output_type_ctx).normalized():
                    raise TypeError('not matching handlers output types: `{}` and `{}`'.format(
                        output_type.to_dhall(),
                        new_output_type.to_dhall(),
                    ))
            output_type = new_output_type
            output_type_ctx = new_output_type_ctx

        if output_type is None:
            raise TypeError('empty merge expression without type annotation')
        else:
            return output_type, output_type_ctx


class NaturalMathExpression(BinaryOperatorExpression):
    def _evaluated(self):
        new = super()._evaluated()
        a = new.arg1
        b = new.arg2
        if isinstance(a, NaturalLiteral) and isinstance(b, NaturalLiteral):
            return NaturalLiteral(self._value(a.value, b.value))
        else:
            return new


class Plus(NaturalMathExpression):
    dhall_operator_string = '+'

    def _value(self, a, b):
        return a + b


class Times(NaturalMathExpression):
    dhall_operator_string = '*'

    def _value(self, a, b):
        return a * b


class Or(BinaryOperatorExpression):
    dhall_operator_string = '||'

    def _evaluated(self):
        new = super()._evaluated()
        a = new.arg1
        b = new.arg2
        if isinstance(a, BooleanLiteral):
            if a.value:
                return BooleanLiteral(True)
            else:
                return b
        elif isinstance(b, BooleanLiteral):
            if b.value:
                return BooleanLiteral(True)
            else:
                return a
        elif a.normalized() == b.normalized():
            return a
        else:
            return new


class And(BinaryOperatorExpression):
    dhall_operator_string = '&&'

    def _evaluated(self):
        new = super()._evaluated()
        a = new.arg1
        b = new.arg2
        if isinstance(a, BooleanLiteral):
            if not a.value:
                return BooleanLiteral(False)
            else:
                return b
        elif isinstance(b, BooleanLiteral):
            if not b.value:
                return BooleanLiteral(False)
            else:
                return a
        elif a.normalized() == b.normalized():
            return a
        else:
            return new


@attr.s(frozen=True, auto_attribs=True)
class ImportExpression(Expression):
    source: Any
    context: ShadowDict = CTX_EMPTY


@attr.s(frozen=True, auto_attribs=True)
class SelectExpression(Expression):
    """Select a field from a record"""
    expression: Expression
    label: str
    context: ShadowDict = CTX_EMPTY

    def _type(self, type_ctx):
        if isinstance(self.expression, UnionType):
            # select from union type yields an union constructor
            self.expression.type(type_ctx)
            return ForAll(
                DEFAULT_VARIABLE_NAME,
                self.expression.alternatives_dict[self.label],
                self.expression,
            ), type_ctx.shadow({
                DEFAULT_VARIABLE_NAME: (None, None, None),
            })

        if isinstance(self.expression, RecordLiteral):
            # select from a record yields record field value
            self.expression.type(type_ctx)
            return self.expression.fields_dict[self.label].type(type_ctx)

        raise TypeError('Can\'t select from {}'.format(self.expression))


@attr.s(frozen=True, auto_attribs=True)
class ProjectionExpression(Expression):
    """Select few field from a record and make a new record out of them"""
    expression: Expression
    labels: [str]
    context: ShadowDict = CTX_EMPTY

    def _type(self, type_ctx):
        expression_type = self.expression.type(type_ctx)
        if not isinstance(expression_type, RecordType):
            raise TypeError('expresion to select fields from must be a record')
        return RecordType([
            (l, expression_type.fields_dict[self.label])
            for l in self.labels
        ]), type_ctx


# literals


@attr.s(frozen=True, auto_attribs=True)
class ListLiteral(Expression):
    items: [Expression]
    element_type: Optional[Expression] = None  # needed for empty lists
    context: ShadowDict = CTX_EMPTY

    def map(self, f):
        return attr.evolve(
            self,
            items=[f(expr) for expr in self.items],
            element_type=None if self.element_type is None else f(self.element_type),
        )

    def to_dhall(self):
        if self.items:
            return '[{}]'.format(', '.join([expr.to_dhall() for expr in self.items]))
        else:
            return '[] : {}'.format(self.element_type.to_dhall())


@attr.s(frozen=True, auto_attribs=True)
class RecordLiteral(Expression):
    fields: [(str, Expression)]
    context: ShadowDict = CTX_EMPTY

    def map(self, f):
        return RecordLiteral([
            (k, f(v))
            for k, v in self.fields
        ])

    def _type(self, type_ctx):
        return RecordType([
            (l, val.type(type_ctx)[0])
            for l, val in self.fields
        ]), type_ctx

    @property
    def fields_dict(self):
        return dict(self.fields)


@attr.s(frozen=True, auto_attribs=True)
class Union(Expression):
    label: str
    value: Expression
    alternatives: [(str, Expression)]
    context: ShadowDict = CTX_EMPTY

    def _type(self, type_ctx):
        if not unique([self.label] + [a[0] for a in self.alternatives]):
            raise TypeError('nonunique union labels')
        # TODO verify that all expressions are types
        return UnionType([(self.label, self.value.type(type_ctx)[0])] + self.alternatives), type_ctx


@attr.s(frozen=True, auto_attribs=True)
class OptionalLiteral(Expression):
    wrapped: Optional[Expression] = None
    context: ShadowDict = CTX_EMPTY


@attr.s(frozen=True, auto_attribs=True)
class DoubleLiteral(Expression):
    value: float
    context: ShadowDict = CTX_EMPTY


@attr.s(frozen=True, auto_attribs=True)
class NaturalLiteral(Expression):
    value: int
    context: ShadowDict = CTX_EMPTY


@attr.s(frozen=True, auto_attribs=True)
class TextLiteral(Expression):
    chunks: [str]
    context: ShadowDict = CTX_EMPTY


@attr.s(frozen=True, auto_attribs=True)
class BooleanLiteral(Expression):
    value: bool
    context: ShadowDict = CTX_EMPTY

    def to_dhall(self):
        return str(self.value)


# ### builtins ###


class BuiltinExpression(Expression):
    builtin_type = None
    dhall_string = None

    def _type(self, type_ctx):
        return self.builtin_type, CTX_EMPTY

    def _normalized(self, ctx):
        return self

    def to_dhall(self):
        return self.dhall_string


class SortBuiltin(BuiltinExpression):
    dhall_string = 'Sort'

    def _type(self, type_ctx):
        raise TypeError('it\'s impossible to infer type of `Sort`')


class KindBuiltin(BuiltinExpression):
    builtin_type = SortBuiltin()
    dhall_string = 'Kind'


class TypeBuiltin(BuiltinExpression):
    builtin_type = KindBuiltin()
    dhall_string = 'Type'


class BoolBuiltin(BuiltinExpression):
    builtin_type = TypeBuiltin()
    dhall_string = 'Bool'


class NaturalBuiltin(BuiltinExpression):
    builtin_type = TypeBuiltin()
    dhall_string = 'Natural'


class DoubleBuiltin(BuiltinExpression):
    builtin_type = TypeBuiltin()
    dhall_string = 'Double'


class TextBuiltin(BuiltinExpression):
    builtin_type = TypeBuiltin()
    dhall_string = 'Text'


class ListBuiltin(BuiltinExpression):
    builtin_type = ForAll(DEFAULT_VARIABLE_NAME, TypeBuiltin(), TypeBuiltin())
    dhall_string = 'List'

    def can_apply_to(self, value):
        return True

    def apply(self, value):
        return ListType(value)


class ListBuild(BuiltinExpression):
    dhall_string = 'List/build'

    def can_apply_to(self, value):
        return True

    def apply(self, value):
        return ListBuildTyped(value)


@attr.s(frozen=True, auto_attribs=True)
class ListBuildTyped(Expression):
    element_type: Expression
    context: ShadowDict = CTX_EMPTY

    @property
    def list_type_expression(self):
        return ListType(self.element_type)

    @property
    def cons_expression(self):
        return Lambda(
            'a', self.element_type,
            Lambda(
                'as', self.list_type_expression,
                ListAppendExpression(
                    ListLiteral([Variable('a')]),
                    Variable('as'),
                ),
            ),
        )

    @property
    def nil_expression(self):
        return ListLiteral([], self.element_type)

    def can_apply_to(self, value):
        return True

    def apply(self, value):
        if (
            isinstance(value, ApplicationExpression) and
            isinstance(value.arg1, ListFoldTyped)
        ):
            return value.arg2

        return ApplicationExpression(
            ApplicationExpression(
                ApplicationExpression(
                    value,
                    self.list_type_expression,
                ),
                self.cons_expression,
            ),
            self.nil_expression,
        ).evaluated()


class ListFold(BuiltinExpression):
    dhall_string = 'List/fold'


@attr.s(frozen=True, auto_attribs=True)
class ListFoldTyped(Expression):
    element_type: Expression
    context: ShadowDict = CTX_EMPTY


class DoubleShowBuiltin(BuiltinExpression):
    builtin_type = ForAll(DEFAULT_VARIABLE_NAME, DoubleBuiltin(), TextBuiltin())
    dhall_string = 'Double/show'

    def can_apply_to(self, value):
        return isinstance(value, DoubleLiteral)

    def apply(self, value):
        return TextLiteral([c for c in str(value.value)])


_builtins = {
    builtin.dhall_string: builtin()
    for builtin in [
        BoolBuiltin,
        # 'Optional': BuiltinNotImplemented,
        # 'None': BuiltinNotImplemented,
        NaturalBuiltin,
        # 'Integer': BuiltinNotImplemented,
        DoubleBuiltin,
        DoubleShowBuiltin,
        TextBuiltin,
        ListBuiltin,
        ListBuild,
        ListFold,
        # 'NaN': BuiltinNotImplemented,
        # 'Infinity': BuiltinNotImplemented,
        TypeBuiltin,
        KindBuiltin,
        SortBuiltin,
    ]
}
_builtins['False'] = BooleanLiteral(False)
_builtins['True'] = BooleanLiteral(True)


def make_builtin_or_variable(name):
    if name in _builtins:
        return _builtins[name]
    return Variable(name)


# types


@attr.s(frozen=True, auto_attribs=True)
class ListType(Expression):
    items_type: Expression
    context: ShadowDict = CTX_EMPTY

    def to_dhall(self):
        return 'List {}'.format(self.items_type.to_dhall())


@attr.s(frozen=True, auto_attribs=True)
class RecordType(Expression):
    fields: [(str, Expression)]
    context: ShadowDict = CTX_EMPTY

    @property
    def fields_dict(self):
        return dict(self.fields)

    def _normalized(self, ctx):
        return RecordType([
            (name, expression.normalized(ctx))
            for name, expression in self.fields
        ])

    def map(self, f):
        return RecordType([
            (name, f(expression))
            for name, expression in self.fields
        ])

    def _type(self, type_ctx):
        if not self.fields:
            return TypeBuiltin(), CTX_EMPTY
        field_types = []
        for name, expression in self.fields:
            typ = expression.normalized_type(type_ctx)
            if typ == SortBuiltin() and not exact(expression, KindBuiltin()):
                raise TypeError("expected `Kind` in a record type field, but got {}".format(
                    expression.to_dhall(),
                ))
            field_types.append(typ)
        if all(t == TypeBuiltin() for t in field_types):
            return TypeBuiltin(), CTX_EMPTY
        if all(t in (KindBuiltin(), TypeBuiltin()) for t in field_types):
            return SortBuiltin(), CTX_EMPTY
        raise TypeError("all record type members must be of type Type, or all must be of type Kind or Sort")


@attr.s(frozen=True, auto_attribs=True)
class UnionType(Expression):
    alternatives: [(str, Expression)]
    context: ShadowDict = CTX_EMPTY

    @property
    def alternatives_dict(self):
        return dict(self.alternatives)

    def _type(self, type_ctx):
        if not unique([a[0] for a in self.alternatives]):
            raise TypeError('fields of union type must be unique')
        if len(self.alternatives) == 0:
            return TypeBuiltin(), CTX_EMPTY
        typ = self.alternatives[0][1].substitute_many(self.context).normalized_type(type_ctx)
        if typ not in (TypeBuiltin(), KindBuiltin(), SortBuiltin()):
            raise TypeError('only Types, Kind and Sorts are allowed for union type alternatives')
        for a in self.alternatives[1:]:
            alternative_typ = a[1].substitute_many(self.context).normalized_type(type_ctx)
            if typ != alternative_typ:
                raise TypeError('all fields on union type must have the same type')
        return typ, CTX_EMPTY

    def _normalized(self, ctx):
        new = super()._normalized(ctx)
        return attr.evolve(
            new,
            alternatives=sorted(new.alternatives),
        )

    def map(self, f):
        return attr.evolve(
            self,
            alternatives=[
                (name, f(expr))
                for name, expr in self.alternatives
            ],
        )


@attr.s(frozen=True, auto_attribs=True)
class OptionalType(Expression):
    wrapped: Expression
    context: ShadowDict = CTX_EMPTY
