"""读取 BOSS 公司文档响应中的首屏状态；仅解释白名单字段所需的数据节点。"""
from html.parser import HTMLParser

from sites.boss import FIELDS, SchemaError


class StateScript(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inline = False
        self.source = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.inline = not dict(attrs).get("src")

    def handle_endtag(self, tag):
        if tag == "script":
            self.inline = False

    def handle_data(self, value):
        if self.inline and value.lstrip().startswith("window.__BOSSCOMPANY__="):
            self.source = value


class StateReader:
    """序列化器将重复值作为形参，并在 return 前给数组下标和对象属性赋值。"""
    def __init__(self, call):
        if call.type != "CallExpression" or call.callee.type != "FunctionExpression":
            raise SchemaError("公司首屏状态封装已变化")
        self.args = {p.name: v for p, v in zip(call.callee.params, call.arguments)}
        self.members = {}
        self.root = None
        for statement in call.callee.body.body:
            if statement.type == "ReturnStatement":
                self.root = statement.argument
                break
            expr = statement.expression
            if expr and expr.type == "AssignmentExpression" and expr.operator == "=":
                target = expr.left
                if target.type == "MemberExpression" and target.object.type == "Identifier":
                    self.members[target.object.name, self.prop(target.property)] = expr.right

    @staticmethod
    def prop(node):
        return node.name if node.type == "Identifier" else node.value

    def arg(self, node):
        if node is not None and node.type == "Identifier":
            return self.args.get(node.name)
        return node

    def get(self, node, key):
        if node is None:
            return None
        if node.type == "Identifier" and (node.name, key) in self.members:
            return self.members[node.name, key]
        node = self.arg(node)
        if node is None:
            return None
        if node.type == "ObjectExpression":
            return next((p.value for p in node.properties if self.prop(p.key) == key), None)
        if node.type == "ArrayExpression" and isinstance(key, int) and key < len(node.elements):
            return node.elements[key]
        return None

    def items(self, node):
        array = self.arg(node)
        if array is not None and array.type == "ArrayExpression":
            length = len(array.elements)
        elif (array is not None and array.type == "CallExpression" and array.callee.name == "Array"
              and len(array.arguments) == 1 and array.arguments[0].type == "Literal"):
            length = array.arguments[0].value
        else:
            raise SchemaError("首屏列表格式不符")
        if type(length) is not int or not 0 <= length <= 2000:
            raise SchemaError("首屏列表长度异常")
        return [self.get(node, i) for i in range(length)]

    def scalar(self, node):
        value = self.arg(node)
        if value is not None and value.type == "Literal":
            return value.value
        if value is not None and value.type == "UnaryExpression" and value.operator == "!":
            return not self.scalar(value.argument)
        raise SchemaError("首屏字段不再是可读取的常量")

    def jobs(self):
        data = self.get(self.get(self.root, "data"), 0)
        info = self.get(data, "brandJobInfo")
        jobs = []
        for row in self.items(self.get(info, "jobList")):
            job = {}
            for name in FIELDS:
                field = self.get(row, name)
                if field is None:
                    continue
                if name == "skills":
                    value = self.arg(field)
                    if value is not None and value.type == "Literal" and value.value is None:
                        job[name] = []
                    else:
                        job[name] = [self.scalar(skill) for skill in self.items(field)]
                else:
                    job[name] = self.scalar(field)
            jobs.append(job)
        result = {"jobList": jobs}
        more = self.get(info, "hasMore")
        if more is not None:
            result["hasMore"] = self.scalar(more)
        return result


def company_list(html):
    parser = StateScript()
    parser.feed(html)
    if not parser.source:
        return None
    import esprima
    try:
        statement, = esprima.parseScript(parser.source).body
        assignment = statement.expression
        if assignment.type != "AssignmentExpression" or assignment.left.property.name != "__BOSSCOMPANY__":
            raise SchemaError("公司首屏状态已变化")
        return StateReader(assignment.right).jobs()
    except (ValueError, AttributeError, TypeError, esprima.Error) as exc:
        raise SchemaError("无法解析公司首屏状态") from exc
