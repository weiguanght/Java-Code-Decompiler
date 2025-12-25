"""
基于 Tree-sitter 的高级 Java 语法解析器模块

功能:
- 容错解析（支持损坏代码）
- 跨类成员解析
- 继承链解析
- 返回值类型追踪
- Query 模式高效匹配
"""

import tree_sitter_java as tsjava
from tree_sitter import Language, Parser
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ==================== 语言和查询 ====================

JAVA_LANGUAGE = Language(tsjava.language())

# Tree-sitter Query 模式
QUERY_METHOD_INVOCATION = """
(method_invocation
  object: (_)? @obj
  name: (identifier) @method
  arguments: (argument_list) @args) @call
"""

QUERY_FIELD_ACCESS = """
(field_access
  object: (_) @obj
  field: (identifier) @field) @access
"""

QUERY_LOCAL_VAR = """
(local_variable_declaration
  type: (_) @type
  declarator: (variable_declarator
    name: (identifier) @name))
"""

QUERY_CLASS_DECL = """
[
  (class_declaration
    name: (identifier) @class_name
    superclass: (superclass (type_identifier) @parent)?
    interfaces: (super_interfaces (type_list (type_identifier) @interface)*)?)
  (interface_declaration
    name: (identifier) @class_name
    interfaces: (extends_interfaces (type_list (type_identifier) @interface)*)?)
]
"""

QUERY_NEW_EXPRESSION = """
(object_creation_expression
  type: (_) @type
  arguments: (argument_list) @args) @new
"""

QUERY_CAST = """
(cast_expression
  type: (_) @type
  value: (_) @value) @cast
"""

QUERY_METHOD_DECL = """
(method_declaration
  type: (_) @return_type
  name: (identifier) @name
  parameters: (formal_parameters) @params) @method
"""

QUERY_FIELD_DECL = """
(field_declaration
  type: (_) @type
  declarator: (variable_declarator
    name: (identifier) @name)) @field
"""

QUERY_IDENTIFIERS = "(identifier) @id"


# ==================== 数据类 ====================

@dataclass
class MethodInfo:
    name: str
    return_type: str
    param_types: List[str]
    param_count: int
    start_byte: int
    end_byte: int


@dataclass
class FieldInfo:
    name: str
    type_name: str
    start_byte: int
    end_byte: int


@dataclass
class LocalVarInfo:
    name: str
    type_name: str
    scope_start: int
    scope_end: int


@dataclass
class ClassTypeInfo:
    class_name: str
    parent_class: str = ""
    interfaces: List[str] = field(default_factory=list)
    fields: Dict[str, FieldInfo] = field(default_factory=dict)
    methods: Dict[str, List[MethodInfo]] = field(default_factory=dict)
    local_vars: List[LocalVarInfo] = field(default_factory=list)
    error_regions: List[Tuple[int, int]] = field(default_factory=list)


# ==================== 全局类型索引 ====================

class GlobalTypeIndex:
    """
    全局类型索引 - 支持跨类成员解析
    """
    
    def __init__(self, class_map: Dict[str, str], member_map: Dict[str, List[dict]]):
        self.class_map = class_map
        self.reverse_class_map = {v: k for k, v in class_map.items()}
        
        # 类型 -> 字段映射 {obf_class: {obf_field: orig_field}}
        self.field_index: Dict[str, Dict[str, str]] = {}
        
        # 字段类型映射 {(obf_class, obf_field): field_type}  # NEW
        self.field_types: Dict[Tuple[str, str], str] = {}
        
        # 类型 -> 方法映射 {obf_class: {obf_method: [method_infos]}}
        self.method_index: Dict[str, Dict[str, List[dict]]] = {}
        
        # 方法返回类型 {(obf_class, obf_method): return_type}
        self.method_returns: Dict[Tuple[str, str], str] = {}
        
        # 继承链 {obf_class: [parent_obf_classes]}
        self.parent_map: Dict[str, List[str]] = {}
        
        # 全局回退映射 {obf_name: orig_name} (用于接口定义缺失映射的情况)
        self.global_field_fallback: Dict[str, str] = {}
        self.global_method_fallback: Dict[str, str] = {}
        
        self._build_indexes(member_map)
        self._build_global_fallbacks(member_map)
    
    def _build_indexes(self, member_map: Dict[str, List[dict]]):
        """构建索引"""
        for obf_class, members in member_map.items():
            self.field_index[obf_class] = {}
            self.method_index[obf_class] = {}
            
            for m in members:
                if m['is_method']:
                    if m['obf'] not in self.method_index[obf_class]:
                        self.method_index[obf_class][m['obf']] = []
                    self.method_index[obf_class][m['obf']].append(m)
                    
                    # 记录返回类型
                    ret_type = m.get('return_type', '')
                    if ret_type and ret_type not in ('void', 'int', 'long', 'float', 'double', 'boolean', 'byte', 'char', 'short'):
                        self.method_returns[(obf_class, m['obf'])] = ret_type
                else:
                    self.field_index[obf_class][m['obf']] = m['orig']
                    # 记录字段类型 (NEW)
                    field_type = m.get('return_type', '')  # mappings.txt 中字段类型存储在 return_type
                    if field_type:
                        self.field_types[(obf_class, m['obf'])] = field_type
    
    def _build_global_fallbacks(self, member_map: Dict[str, List[dict]]):
        """构建全局解析回退表（含返回类型信息）"""
        from collections import Counter
        field_counts = {} # obf -> Counter(orig)
        method_counts = {} # obf -> Counter(orig)
        # 基于返回类型的方法回退 {(obf, ret_type): Counter(orig)}
        method_by_sig = {}
        
        for obf_class, members in member_map.items():
            for m in members:
                obf = m['obf']
                orig = m['orig']
                if m['is_method']:
                    if obf not in method_counts: method_counts[obf] = Counter()
                    method_counts[obf][orig] += 1
                    
                    # 按返回类型分组
                    ret_type = m.get('return_type', 'void')
                    # 简化返回类型（仅保留基础类型或短名）
                    simple_ret = ret_type.split('.')[-1] if ret_type else 'void'
                    key = (obf, simple_ret)
                    if key not in method_by_sig: method_by_sig[key] = Counter()
                    method_by_sig[key][orig] += 1
                else:
                    if obf not in field_counts: field_counts[obf] = Counter()
                    field_counts[obf][orig] += 1
        
        for obf, counts in field_counts.items():
            self.global_field_fallback[obf] = counts.most_common(1)[0][0]
        for obf, counts in method_counts.items():
            self.global_method_fallback[obf] = counts.most_common(1)[0][0]
        
        # 存储基于签名的回退 {(obf, ret_type): orig}
        self.method_by_signature = {}
        for (obf, ret), counts in method_by_sig.items():
            self.method_by_signature[(obf, ret)] = counts.most_common(1)[0][0]

    def set_inheritance(self, child: str, parent: str):
        """设置继承关系（支持多接口）"""
        if child not in self.parent_map:
            self.parent_map[child] = []
        if parent not in self.parent_map[child]:
            self.parent_map[child].append(parent)
    
    def resolve_field(self, obj_type: str, field_name: str) -> Optional[str]:
        """解析字段名（支持多重继承和回退）"""
        if not obj_type: return None  # 禁用全局回退，避免跨类污染
        
        queue = [obj_type]
        visited = set()
        
        while queue:
            current = queue.pop(0)
            if current in visited: continue
            visited.add(current)
            
            if current in self.field_index and field_name in self.field_index[current]:
                return self.field_index[current][field_name]
            
            parents = self.parent_map.get(current, [])
            queue.extend(parents)
            
        # 禁用全局回退：仅在明确知道类型时替换
        return None
    
    def resolve_method(self, obj_type: str, method_name: str, arg_count: int = -1) -> Optional[str]:
        """解析方法名（支持多重继承、重载和回退）"""
        if not obj_type: return None  # 禁用全局回退，避免跨类污染

        queue = [obj_type]
        visited = set()
        
        while queue:
            current = queue.pop(0)
            if current in visited: continue
            visited.add(current)
            
            if current in self.method_index and method_name in self.method_index[current]:
                methods = self.method_index[current][method_name]
                if arg_count >= 0:
                    for m in methods:
                        if self._count_params(m.get('signature', '')) == arg_count:
                            return m['orig']
                return methods[0]['orig']
            
            parents = self.parent_map.get(current, [])
            queue.extend(parents)
            
        # 禁用全局回退：仅在明确知道类型时替换
        return None
    
    def get_method_return_type(self, obj_type: str, method_name: str) -> Optional[str]:
        """获取方法返回类型"""
        return self.method_returns.get((obj_type, method_name))
    
    def get_field_type(self, obj_type: str, field_name: str) -> Optional[str]:
        """获取字段类型（支持继承链遍历）"""
        if not obj_type:
            return None
        
        queue = [obj_type]
        visited = set()
        
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            
            # 查找当前类的字段类型
            field_type = self.field_types.get((current, field_name))
            if field_type:
                return field_type
            
            # 继续查找父类
            parents = self.parent_map.get(current, [])
            queue.extend(parents)
        
        return None
    
    def _count_params(self, signature: str) -> int:
        if not signature:
            return 0
        inner = signature.strip('()')
        if not inner:
            return 0
        return len(inner.split(','))


# ==================== 全局类型索引单例 ====================

_global_type_index: Optional[GlobalTypeIndex] = None
_global_type_index_lock = None  # 可选：线程安全

def get_global_type_index() -> Optional[GlobalTypeIndex]:
    """获取全局类型索引单例"""
    return _global_type_index

def init_global_type_index(class_map: Dict[str, str], member_map: Dict[str, List[dict]], 
                           force_rebuild: bool = False) -> GlobalTypeIndex:
    """
    初始化或获取全局类型索引单例
    
    Args:
        class_map: 类映射表 {obf: orig}
        member_map: 成员映射表 {obf_class: [member_info]}
        force_rebuild: 是否强制重建（默认只构建一次）
        
    Returns:
        GlobalTypeIndex 实例
    """
    global _global_type_index
    
    if _global_type_index is None or force_rebuild:
        _global_type_index = GlobalTypeIndex(class_map, member_map)
    
    return _global_type_index

def reset_global_type_index():
    """重置全局类型索引（用于测试）"""
    global _global_type_index
    _global_type_index = None


# ==================== Tree-sitter 解析器 ====================

class TreeSitterJavaParser:
    """
    高级 Java 解析器 - 使用 Query 模式
    """
    
    def __init__(self):
        self.parser = Parser(JAVA_LANGUAGE)
        self.language = JAVA_LANGUAGE
        
        # 编译查询
        self._query_method_call = self.language.query(QUERY_METHOD_INVOCATION)
        self._query_field_access = self.language.query(QUERY_FIELD_ACCESS)
        self._query_local_var = self.language.query(QUERY_LOCAL_VAR)
        self._query_class_decl = self.language.query(QUERY_CLASS_DECL)
        self._query_new_expr = self.language.query(QUERY_NEW_EXPRESSION)
        self._query_cast = self.language.query(QUERY_CAST)
        self._query_method_decl = self.language.query(QUERY_METHOD_DECL)
        self._query_field_decl = self.language.query(QUERY_FIELD_DECL)
        self._query_identifiers = self.language.query(QUERY_IDENTIFIERS)
    
    def parse(self, code: str):
        return self.parser.parse(bytes(code, 'utf8'))
    
    def extract_type_info(self, code: str) -> ClassTypeInfo:
        """提取类型信息"""
        tree = self.parse(code)
        root = tree.root_node
        code_bytes = bytes(code, 'utf8')
        
        info = ClassTypeInfo(class_name="")
        
        # 收集错误节点
        self._collect_errors(root, info.error_regions)
        
        # 使用 Query 提取类声明 (API: Dict[str, List[Node]])
        try:
            captures = self._query_class_decl.captures(root)
            if 'class_name' in captures and captures['class_name']:
                info.class_name = self._node_text(captures['class_name'][0], code_bytes)
            if 'parent' in captures and captures['parent']:
                info.parent_class = self._node_text(captures['parent'][0], code_bytes)
            for node in captures.get('interface', []):
                info.interfaces.append(self._node_text(node, code_bytes))
        except:
            pass
        
        # 提取局部变量
        try:
            captures = self._query_local_var.captures(root)
            types = captures.get('type', [])
            names = captures.get('name', [])
            for t_node, n_node in zip(types, names):
                info.local_vars.append(LocalVarInfo(
                    name=self._node_text(n_node, code_bytes),
                    type_name=self._node_text(t_node, code_bytes),
                    scope_start=0,
                    scope_end=len(code)
                ))
        except:
            pass
        # 提取字段声明
        try:
            captures = self._query_field_decl.captures(root)
            types = captures.get('type', [])
            names = captures.get('name', [])
            for t_node, n_node in zip(types, names):
                name = self._node_text(n_node, code_bytes)
                info.fields[name] = FieldInfo(
                    name=name,
                    type_name=self._node_text(t_node, code_bytes),
                    start_byte=n_node.start_byte,
                    end_byte=n_node.end_byte
                )
        except:
            pass
            
        # 提取方法声明
        try:
            captures = self._query_method_decl.captures(root)
            ret_types = captures.get('return_type', [])
            names = captures.get('name', [])
            params = captures.get('params', [])
            for r_node, n_node, p_node in zip(ret_types, names, params):
                name = self._node_text(n_node, code_bytes)
                m_info = MethodInfo(
                    name=name,
                    return_type=self._node_text(r_node, code_bytes),
                    param_types=[], # 简化：暂不提取参数具体类型
                    param_count=self._count_args(p_node),
                    start_byte=n_node.start_byte,
                    end_byte=n_node.end_byte
                )
                if name not in info.methods:
                    info.methods[name] = []
                info.methods[name].append(m_info)
        except:
            pass
            
        return info
    
    def _node_text(self, node, code_bytes: bytes) -> str:
        return code_bytes[node.start_byte:node.end_byte].decode('utf8')
    
    def find_method_calls_query(self, code: str) -> List[dict]:
        """使用 Query 查找所有方法调用"""
        tree = self.parse(code)
        code_bytes = bytes(code, 'utf8')
        results = []
        
        try:
            captures = self._query_method_call.captures(tree.root_node)
            calls = captures.get('call', [])
            methods = captures.get('method', [])
            objs = captures.get('obj', [])
            args_list = captures.get('args', [])
            
            # 按位置匹配
            for i, call_node in enumerate(calls):
                result = {
                    'start_byte': call_node.start_byte,
                    'end_byte': call_node.end_byte,
                    'full_text': self._node_text(call_node, code_bytes)
                }
                
                # 查找属于此调用的子节点
                for m_node in methods:
                    if call_node.start_byte <= m_node.start_byte <= call_node.end_byte:
                        result['name'] = self._node_text(m_node, code_bytes)
                        result['name_start'] = m_node.start_byte
                        result['name_end'] = m_node.end_byte
                        break
                
                for o_node in objs:
                    if call_node.start_byte <= o_node.start_byte <= call_node.end_byte:
                        result['obj'] = self._node_text(o_node, code_bytes)
                        result['obj_start'] = o_node.start_byte
                        break
                
                for a_node in args_list:
                    if call_node.start_byte <= a_node.start_byte <= call_node.end_byte:
                        result['args'] = self._node_text(a_node, code_bytes)
                        result['arg_count'] = self._count_args(a_node)
                        break
                
                if 'name' in result:
                    results.append(result)
        except:
            pass
        
        return results
    
    def find_field_accesses_query(self, code: str) -> List[dict]:
        """使用 Query 查找所有字段访问"""
        tree = self.parse(code)
        code_bytes = bytes(code, 'utf8')
        results = []
        
        try:
            captures = self._query_field_access.captures(tree.root_node)
            accesses = captures.get('access', [])
            objs = captures.get('obj', [])
            fields = captures.get('field', [])
            
            for access_node in accesses:
                result = {
                    'start_byte': access_node.start_byte,
                    'end_byte': access_node.end_byte,
                    'full_text': self._node_text(access_node, code_bytes)
                }
                
                for o_node in objs:
                    if access_node.start_byte <= o_node.start_byte <= access_node.end_byte:
                        result['obj'] = self._node_text(o_node, code_bytes)
                        break
                
                for f_node in fields:
                    if access_node.start_byte <= f_node.start_byte <= access_node.end_byte:
                        result['field'] = self._node_text(f_node, code_bytes)
                        result['field_start'] = f_node.start_byte
                        result['field_end'] = f_node.end_byte
                        break
                
                if 'field' in result:
                    results.append(result)
        except:
            pass
        
        return results
    
    def find_new_expressions(self, code: str) -> List[dict]:
        """查找 new 表达式"""
        tree = self.parse(code)
        code_bytes = bytes(code, 'utf8')
        results = []
        
        try:
            captures = self._query_new_expr.captures(tree.root_node)
            news = captures.get('new', [])
            types = captures.get('type', [])
            args_list = captures.get('args', [])
            
            for new_node in news:
                result = {'start_byte': new_node.start_byte, 'end_byte': new_node.end_byte}
                
                for t_node in types:
                    if new_node.start_byte <= t_node.start_byte <= new_node.end_byte:
                        result['type'] = self._node_text(t_node, code_bytes)
                        break
                
                for a_node in args_list:
                    if new_node.start_byte <= a_node.start_byte <= new_node.end_byte:
                        result['arg_count'] = self._count_args(a_node)
                        break
                
                if 'type' in result:
                    results.append(result)
        except:
            pass
        
        return results
    
    def find_casts(self, code: str) -> List[dict]:
        """查找类型转换"""
        tree = self.parse(code)
        code_bytes = bytes(code, 'utf8')
        results = []
        
        try:
            captures = self._query_cast.captures(tree.root_node)
            casts = captures.get('cast', [])
            types = captures.get('type', [])
            values = captures.get('value', [])
            
            for cast_node in casts:
                result = {'start_byte': cast_node.start_byte, 'end_byte': cast_node.end_byte}
                
                for t_node in types:
                    if cast_node.start_byte <= t_node.start_byte <= cast_node.end_byte:
                        result['type'] = self._node_text(t_node, code_bytes)
                        break
                
                for v_node in values:
                    if cast_node.start_byte <= v_node.start_byte <= cast_node.end_byte:
                        result['value'] = self._node_text(v_node, code_bytes)
                        break
                
                if 'type' in result:
                    results.append(result)
        except:
            pass
        
        return results
    
    def _count_args(self, args_node) -> int:
        count = 0
        for child in args_node.children:
            if child.type not in ('(', ')', ','):
                count += 1
        return count
    
    def _collect_errors(self, node, error_regions: List[Tuple[int, int]]):
        if node.type == 'ERROR' or node.is_missing:
            error_regions.append((node.start_byte, node.end_byte))
        for child in node.children:
            self._collect_errors(child, error_regions)

    def find_identifiers_in_errors(self, root, error_regions: List[Tuple[int, int]], code_bytes: bytes) -> List[dict]:
        """寻找错误区域中的标识符"""
        results = []
        if not error_regions:
            return results
            
        try:
            captures = self._query_identifiers.captures(root)
            for node in captures.get('id', []):
                if self.is_in_error_region(node.start_byte, error_regions):
                    results.append({
                        'name': self._node_text(node, code_bytes),
                        'start': node.start_byte,
                        'end': node.end_byte
                    })
        except:
            pass
        return results
    
    def is_in_error_region(self, pos: int, regions: List[Tuple[int, int]]) -> bool:
        return any(s <= pos <= e for s, e in regions)
    
    def find_method_declarations(self, code: str) -> List[dict]:
        """查找方法声明"""
        results = []
        try:
            tree = self.parse(code)
            captures = self._query_method_decl.captures(tree.root_node)
            code_bytes = bytes(code, 'utf8')
            
            methods = captures.get('method', [])
            names = captures.get('name', [])
            
            for m_node in methods:
                res = {'start_byte': m_node.start_byte, 'end_byte': m_node.end_byte}
                for n_node in names:
                    if m_node.start_byte <= n_node.start_byte <= m_node.end_byte:
                        res['name'] = self._node_text(n_node, code_bytes)
                        res['name_start'] = n_node.start_byte
                        res['name_end'] = n_node.end_byte
                        break
                if 'name' in res:
                    results.append(res)
        except:
            pass
        return results

    def find_field_declarations(self, code: str) -> List[dict]:
        """查找字段声明"""
        results = []
        try:
            tree = self.parse(code)
            captures = self._query_field_decl.captures(tree.root_node)
            code_bytes = bytes(code, 'utf8')
            
            fields = captures.get('field', [])
            names = captures.get('name', [])
            
            for f_node in fields:
                res = {'start_byte': f_node.start_byte, 'end_byte': f_node.end_byte}
                for n_node in names:
                    if f_node.start_byte <= n_node.start_byte <= f_node.end_byte:
                        res['name'] = self._node_text(n_node, code_bytes)
                        res['name_start'] = n_node.start_byte
                        res['name_end'] = n_node.end_byte
                        break
                if 'name' in res:
                    results.append(res)
        except:
            pass
        return results


# ==================== 便捷函数 ====================

def parse_java_code(code: str):
    return TreeSitterJavaParser().parse(code)


def extract_type_info(code: str) -> ClassTypeInfo:
    return TreeSitterJavaParser().extract_type_info(code)


def count_errors(code: str) -> int:
    info = TreeSitterJavaParser().extract_type_info(code)
    return len(info.error_regions)

