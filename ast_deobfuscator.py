"""
AST-First 反混淆引擎

核心流程:
1. 输入纯净的 JADX 原始代码（无任何预处理）
2. Tree-sitter 直接解析 AST
3. 遍历 AST 收集所有需替换的节点 (start_byte, end_byte, replacement_text)
4. 按 start_byte 倒序一次性应用替换
5. 无需占位符和字符串保护逻辑（Tree-sitter 天然区分字符串和标识符）

优势:
- 避免正则破坏语法结构
- 通过 AST 节点精确定位标识符
- 延迟替换避免位置漂移
- 上下文感知区分字段/方法/类
"""

import re
from typing import Dict, List, Tuple, Optional, Set
from dataclasses import dataclass, field

# 尝试导入 tree-sitter
try:
    import tree_sitter
    import tree_sitter_java as tsjava
    from tree_sitter import Parser, Language
    TREE_SITTER_AVAILABLE = True
    JAVA_LANGUAGE = Language(tsjava.language())
except ImportError:
    TREE_SITTER_AVAILABLE = False
    JAVA_LANGUAGE = None


# ==================== TextEdit 数据结构 ====================

@dataclass
class TextEdit:
    """单个文本编辑操作"""
    start_byte: int
    end_byte: int
    new_text: str
    reason: str = ""  # 调试用


class TextEdits:
    """
    文本编辑集合
    
    收集所有编辑操作，按 start_byte 倒序一次性应用，避免位置漂移
    """
    
    def __init__(self):
        self.edits: List[TextEdit] = []
        self._positions: Set[Tuple[int, int]] = set()
    
    def add(self, start: int, end: int, text: str, reason: str = ""):
        """
        添加编辑操作（自动去重）
        
        Args:
            start: 起始字节位置
            end: 结束字节位置
            text: 替换文本
            reason: 替换原因（调试用）
        """
        pos = (start, end)
        if pos not in self._positions:
            self._positions.add(pos)
            self.edits.append(TextEdit(start, end, text, reason))
    
    def apply(self, source: bytes) -> str:
        """
        应用所有编辑操作
        
        按 start_byte 倒序排序后应用，避免位置漂移
        
        Args:
            source: 原始代码字节
            
        Returns:
            处理后的代码字符串
        """
        if not self.edits:
            return source.decode('utf8')
        
        # 按 start_byte 倒序排序
        self.edits.sort(key=lambda e: e.start_byte, reverse=True)
        
        result = bytearray(source)
        for edit in self.edits:
            result[edit.start_byte:edit.end_byte] = edit.new_text.encode('utf8')
        
        return result.decode('utf8')
    
    def __len__(self):
        return len(self.edits)
    
    def debug_dump(self) -> str:
        """调试输出"""
        lines = [f"Total edits: {len(self.edits)}"]
        for e in self.edits[:20]:  # 最多显示 20 条
            lines.append(f"  [{e.start_byte}:{e.end_byte}] -> '{e.new_text}' ({e.reason})")
        if len(self.edits) > 20:
            lines.append(f"  ... and {len(self.edits) - 20} more")
        return '\n'.join(lines)


# ==================== AST 错误检测 ====================

class ASTParseError(Exception):
    """AST 解析错误"""
    pass


def count_nodes(node, node_type: str = None) -> int:
    """递归计算节点数量"""
    count = 1 if (node_type is None or node.type == node_type) else 0
    for child in node.children:
        count += count_nodes(child, node_type)
    return count


def get_error_ratio(root_node) -> float:
    """计算 ERROR 节点比例"""
    total = count_nodes(root_node)
    errors = count_nodes(root_node, 'ERROR')
    return errors / total if total > 0 else 0.0


# ==================== AST 反混淆引擎 ====================

class ASTDeobfuscator:
    """
    AST-First 反混淆引擎
    
    直接在 AST 层面进行类型推断和名称替换
    """
    
    # 错误节点阈值：超过此比例则回退到正则处理
    ERROR_THRESHOLD = 0.1
    
    def __init__(self, class_map: Dict[str, str], member_map: Dict[str, List[dict]], 
                 type_index=None):
        """
        Args:
            class_map: {obf_class: orig_class}
            member_map: {obf_class: [member_info]}
            type_index: GlobalTypeIndex 实例（用于继承链解析）
        """
        self.class_map = class_map
        self.member_map = member_map
        self.type_index = type_index
        
        # 构建短类名映射 {short_obf: [(full_obf, short_orig), ...]}
        # 支持同名短类名的冲突处理
        self._short_class_candidates: Dict[str, List[Tuple[str, str]]] = {}
        for obf, orig in class_map.items():
            obf_short = obf.split('.')[-1]
            orig_short = orig.split('.')[-1]
            if obf_short != orig_short:
                if obf_short not in self._short_class_candidates:
                    self._short_class_candidates[obf_short] = []
                self._short_class_candidates[obf_short].append((obf, orig_short))
        
        # 无冲突的简单映射（用于快速查找）
        self.short_class_map: Dict[str, str] = {}
        for short, candidates in self._short_class_candidates.items():
            if len(candidates) == 1:
                self.short_class_map[short] = candidates[0][1]
        
        # 初始化解析器
        if TREE_SITTER_AVAILABLE:
            self.parser = Parser(JAVA_LANGUAGE)
        else:
            self.parser = None
    
    def process(self, code: str, current_class: str) -> str:
        """
        处理代码进行反混淆
        
        Args:
            code: 原始 Java 代码（纯净，无预处理）
            current_class: 当前类的混淆全限定名
            
        Returns:
            反混淆后的代码
            
        Raises:
            ASTParseError: AST 解析错误过多时抛出
        """
        if not self.parser:
            raise ASTParseError("Tree-sitter not available")
        
        # 1. 解析 AST
        code_bytes = code.encode('utf8')
        tree = self.parser.parse(code_bytes)
        
        # 2. 检查错误比例
        error_ratio = get_error_ratio(tree.root_node)
        if error_ratio > self.ERROR_THRESHOLD:
            raise ASTParseError(f"Too many errors: {error_ratio:.1%}")
        
        # 3. 收集替换项
        edits = TextEdits()
        var_types: Dict[str, str] = {'this': current_class}
        
        self._visit_node(tree.root_node, code_bytes, current_class, var_types, edits)
        
        # 4. 应用替换
        return edits.apply(code_bytes)
    
    def _visit_node(self, node, code_bytes: bytes, current_class: str,
                    var_types: Dict[str, str], edits: TextEdits):
        """递归遍历 AST 节点"""
        
        # 处理类声明（类名替换）
        if node.type in ('class_declaration', 'interface_declaration', 'enum_declaration'):
            self._handle_class_declaration(node, code_bytes, current_class, edits)
        
        # 处理构造函数声明（构造函数名需要与类名同步）
        elif node.type == 'constructor_declaration':
            self._handle_constructor_declaration(node, code_bytes, current_class, edits)
        
        # 处理 import 声明
        elif node.type == 'import_declaration':
            self._handle_import_declaration(node, code_bytes, edits)
        
        # 处理局部变量声明 - 更新类型上下文
        elif node.type == 'local_variable_declaration':
            self._handle_local_var_decl(node, code_bytes, var_types)
        
        # 处理形参声明
        elif node.type == 'formal_parameter':
            self._handle_formal_param(node, code_bytes, var_types)
        
        # 处理方法调用
        elif node.type == 'method_invocation':
            self._handle_method_invocation(node, code_bytes, current_class, var_types, edits)
        
        # 处理字段访问
        elif node.type == 'field_access':
            self._handle_field_access(node, code_bytes, current_class, var_types, edits)
        
        # 处理全限定类型标识符 (com.example.ClassName)
        elif node.type == 'scoped_type_identifier':
            self._handle_scoped_type_identifier(node, code_bytes, edits)
        
        # 处理类型标识符（短类名引用）
        elif node.type == 'type_identifier':
            self._handle_type_identifier(node, code_bytes, edits)
        
        # 处理字符串字面量（反射 - 仅限明确上下文）
        elif node.type == 'string_literal':
            self._handle_string_literal(node, code_bytes, edits)
        
        # 递归处理子节点
        for child in node.children:
            self._visit_node(child, code_bytes, current_class, var_types, edits)
    
    # ==================== 节点处理器 ====================
    
    def _handle_local_var_decl(self, node, code_bytes: bytes, var_types: Dict[str, str]):
        """处理局部变量声明，提取变量类型"""
        type_node = None
        for child in node.children:
            if child.type in ('type_identifier', 'generic_type', 'array_type',
                              'integral_type', 'floating_point_type', 'boolean_type'):
                type_node = child
                break
        
        if not type_node:
            return
        
        type_name = self._get_type_name(type_node, code_bytes)
        
        # 查找变量声明器
        for child in node.children:
            if child.type == 'variable_declarator':
                name_node = child.child_by_field_name('name')
                if name_node:
                    var_name = self._node_text(name_node, code_bytes)
                    var_types[var_name] = type_name
    
    def _handle_formal_param(self, node, code_bytes: bytes, var_types: Dict[str, str]):
        """处理形参声明"""
        type_node = None
        name_node = None
        
        for child in node.children:
            if child.type in ('type_identifier', 'generic_type', 'array_type',
                              'integral_type', 'floating_point_type', 'boolean_type'):
                type_node = child
            elif child.type == 'identifier':
                name_node = child
        
        if type_node and name_node:
            type_name = self._get_type_name(type_node, code_bytes)
            var_name = self._node_text(name_node, code_bytes)
            var_types[var_name] = type_name
    
    def _handle_method_invocation(self, node, code_bytes: bytes, current_class: str,
                                   var_types: Dict[str, str], edits: TextEdits):
        """处理方法调用"""
        name_node = node.child_by_field_name('name')
        if not name_node:
            return
        
        method_name = self._node_text(name_node, code_bytes)
        
        # 获取接收者对象
        obj_node = node.child_by_field_name('object')
        if obj_node:
            obj_type = self._resolve_expression_type(obj_node, code_bytes, var_types, current_class)
        else:
            obj_type = current_class
        
        # 查找方法映射
        orig_name = self._resolve_method(obj_type, method_name, node, code_bytes)
        
        if orig_name and orig_name != method_name:
            edits.add(
                name_node.start_byte,
                name_node.end_byte,
                orig_name,
                f"method: {obj_type}.{method_name} -> {orig_name}"
            )
    
    def _handle_field_access(self, node, code_bytes: bytes, current_class: str,
                              var_types: Dict[str, str], edits: TextEdits):
        """处理字段访问"""
        field_node = node.child_by_field_name('field')
        if not field_node:
            return
        
        field_name = self._node_text(field_node, code_bytes)
        
        # 排除方法调用（父节点是 method_invocation 且当前节点是 object）
        parent = node.parent
        if parent and parent.type == 'method_invocation':
            parent_obj = parent.child_by_field_name('object')
            if parent_obj and parent_obj.id == node.id:
                # 这是方法调用的接收者部分，需要处理中间字段
                pass
            else:
                # 检查是否当前 field 是方法名（后跟括号）
                # field_access 的 field 作为 method_invocation 的 name 不会进入这里
                pass
        
        # 获取对象类型
        obj_node = node.child_by_field_name('object')
        if not obj_node:
            return
        
        obj_type = self._resolve_expression_type(obj_node, code_bytes, var_types, current_class)
        
        # 查找字段映射
        orig_name = self._resolve_field(obj_type, field_name)
        
        if orig_name and orig_name != field_name:
            edits.add(
                field_node.start_byte,
                field_node.end_byte,
                orig_name,
                f"field: {obj_type}.{field_name} -> {orig_name}"
            )
    
    def _handle_constructor_declaration(self, node, code_bytes: bytes, current_class: str, edits: TextEdits):
        """
        处理构造函数声明
        
        构造函数名必须与类名一致，当类名被替换时构造函数名也需要同步替换
        """
        # 查找构造函数名节点
        name_node = node.child_by_field_name('name')
        if not name_node:
            return
        
        constructor_name = self._node_text(name_node, code_bytes)
        
        # 使用 current_class 上下文查找映射
        if current_class in self.class_map:
            orig_full = self.class_map[current_class]
            orig_short = orig_full.split('.')[-1]
            current_short = current_class.split('.')[-1]
            if current_short == constructor_name and current_short != orig_short:
                edits.add(
                    name_node.start_byte,
                    name_node.end_byte,
                    orig_short,
                    f"constructor: {constructor_name} -> {orig_short}"
                )
        # 回退：在短类名映射中查找
        elif constructor_name in self.short_class_map:
            orig_name = self.short_class_map[constructor_name]
            edits.add(
                name_node.start_byte,
                name_node.end_byte,
                orig_name,
                f"constructor: {constructor_name} -> {orig_name}"
            )
    
    def _handle_class_declaration(self, node, code_bytes: bytes, current_class: str, edits: TextEdits):
        """
        处理类/接口/枚举声明
        
        替换类名声明中的混淆名称，使用 current_class 上下文避免短类名冲突
        """
        # 查找类名节点
        name_node = node.child_by_field_name('name')
        if not name_node:
            return
        
        class_name = self._node_text(name_node, code_bytes)
        
        # 优先使用 current_class 上下文查找映射
        if current_class in self.class_map:
            orig_full = self.class_map[current_class]
            orig_short = orig_full.split('.')[-1]
            current_short = current_class.split('.')[-1]
            if current_short == class_name and current_short != orig_short:
                edits.add(
                    name_node.start_byte,
                    name_node.end_byte,
                    orig_short,
                    f"class decl (context): {class_name} -> {orig_short}"
                )
        # 回退：在短类名映射中查找（仅限无冲突的类名）
        elif class_name in self.short_class_map:
            orig_name = self.short_class_map[class_name]
            edits.add(
                name_node.start_byte,
                name_node.end_byte,
                orig_name,
                f"class decl: {class_name} -> {orig_name}"
            )
        
        # 处理 extends/implements 中的父类/接口
        superclass_node = node.child_by_field_name('superclass')
        if superclass_node:
            self._handle_superclass(superclass_node, code_bytes, current_class, edits)
        
        interfaces_node = node.child_by_field_name('interfaces')
        if interfaces_node:
            self._handle_interfaces(interfaces_node, code_bytes, current_class, edits)
    
    def _handle_superclass(self, node, code_bytes: bytes, current_class: str, edits: TextEdits):
        """处理 extends 子句中的父类"""
        # superclass 节点的子节点是类型节点
        for child in node.children:
            if child.type == 'type_identifier':
                type_name = self._node_text(child, code_bytes)
                if type_name in self.short_class_map:
                    edits.add(
                        child.start_byte,
                        child.end_byte,
                        self.short_class_map[type_name],
                        f"extends: {type_name} -> {self.short_class_map[type_name]}"
                    )
            elif child.type == 'scoped_type_identifier':
                self._handle_scoped_type_identifier(child, code_bytes, edits)
    
    def _handle_interfaces(self, node, code_bytes: bytes, current_class: str, edits: TextEdits):
        """处理 implements 子句中的接口"""
        # 遍历所有类型节点
        for child in node.children:
            if child.type == 'type_identifier':
                type_name = self._node_text(child, code_bytes)
                if type_name in self.short_class_map:
                    edits.add(
                        child.start_byte,
                        child.end_byte,
                        self.short_class_map[type_name],
                        f"implements: {type_name} -> {self.short_class_map[type_name]}"
                    )
            elif child.type in ('scoped_type_identifier', 'type_list'):
                if child.type == 'type_list':
                    self._handle_interfaces(child, code_bytes, current_class, edits)  # 递归处理
                else:
                    self._handle_scoped_type_identifier(child, code_bytes, edits)
    
    def _handle_scoped_type_identifier(self, node, code_bytes: bytes, edits: TextEdits):
        """
        处理全限定类型标识符 (com.example.ClassName)
        
        在 Tree-sitter 中，全限定类名表示为嵌套的 scoped_type_identifier
        """
        full_type = self._node_text(node, code_bytes)
        
        # 在完整类映射中查找
        if full_type in self.class_map:
            new_type = self.class_map[full_type]
            edits.add(
                node.start_byte,
                node.end_byte,
                new_type,
                f"scoped type: {full_type} -> {new_type}"
            )
            return
        
        # 尝试替换最后一个部分（短类名）
        # scoped_type_identifier 的最后一个子节点通常是 type_identifier
        for child in reversed(node.children):
            if child.type == 'type_identifier':
                type_name = self._node_text(child, code_bytes)
                if type_name in self.short_class_map:
                    edits.add(
                        child.start_byte,
                        child.end_byte,
                        self.short_class_map[type_name],
                        f"scoped type suffix: {type_name} -> {self.short_class_map[type_name]}"
                    )
                break
    
    def _handle_type_identifier(self, node, code_bytes: bytes, edits: TextEdits):
        """处理类型标识符（短类名）"""
        type_name = self._node_text(node, code_bytes)
        
        # 跳过已经处理过的全限定名部分
        parent = node.parent
        if parent and parent.type == 'scoped_type_identifier':
            return  # scoped_type_identifier 已单独处理
        
        # 跳过类声明中的类名（已由 _handle_class_declaration 处理）
        if parent and parent.type in ('class_declaration', 'interface_declaration', 'enum_declaration'):
            return
        
        # 在短类名映射中查找
        if type_name in self.short_class_map:
            orig_name = self.short_class_map[type_name]
            edits.add(
                node.start_byte,
                node.end_byte,
                orig_name,
                f"type: {type_name} -> {orig_name}"
            )
    
    def _handle_string_literal(self, node, code_bytes: bytes, edits: TextEdits):
        """
        处理字符串字面量中的反射引用
        
        注意：采用保守策略，仅在明确的反射上下文中替换，避免误替换普通字符串。
        - Class.forName("com.example.ClassName") - 仅替换完整类名
        - getMethod/getDeclaredMethod - 仅第一个参数且必须是已知方法名
        """
        # 获取字符串内容（去除引号）
        full_text = self._node_text(node, code_bytes)
        if len(full_text) < 2:
            return
        string_content = full_text[1:-1]  # 去除首尾引号
        
        # 空字符串或过短（单字符方法名太危险）
        if not string_content or len(string_content) < 2:
            return
        
        # 检查是否在反射上下文中
        parent = node.parent
        if not parent or parent.type != 'argument_list':
            return
        
        grandparent = parent.parent
        if not grandparent or grandparent.type != 'method_invocation':
            return
        
        method_name_node = grandparent.child_by_field_name('name')
        if not method_name_node:
            return
        
        method_name = self._node_text(method_name_node, code_bytes)
        
        # === Class.forName("...") ===
        # 额外验证：检查接收者是否是 Class 或 java.lang.Class
        if method_name == 'forName':
            obj_node = grandparent.child_by_field_name('object')
            if obj_node:
                obj_text = self._node_text(obj_node, code_bytes)
                # 仅接受 Class.forName 或 java.lang.Class.forName
                if obj_text not in ('Class', 'java.lang.Class'):
                    return
            
            # 仅替换完整限定类名（包含点号）
            if '.' in string_content and string_content in self.class_map:
                new_content = self.class_map[string_content]
                edits.add(
                    node.start_byte + 1,
                    node.end_byte - 1,
                    new_content,
                    f"reflection: Class.forName({string_content}) -> {new_content}"
                )
        
        # === getMethod/getDeclaredMethod ===
        # 仅处理第一个参数
        elif method_name in ('getMethod', 'getDeclaredMethod'):
            # 检查是否是第一个参数
            args = [c for c in parent.children if c.type not in ('(', ')', ',')]
            if not args or args[0].id != node.id:
                return  # 不是第一个参数，跳过
            
            # 保守策略：仅替换在映射中明确存在的方法名
            for members in self.member_map.values():
                for m in members:
                    if m['is_method'] and m['obf'] == string_content:
                        edits.add(
                            node.start_byte + 1,
                            node.end_byte - 1,
                            m['orig'],
                            f"reflection: {method_name}({string_content}) -> {m['orig']}"
                        )
                        return
    
    def _handle_import_declaration(self, node, code_bytes: bytes, edits: TextEdits):
        """
        处理 import 声明
        
        import 语句结构:
        - import_declaration
          - "import"
          - scoped_identifier (com.example.ClassName)
          - ";"
        """
        # 查找 scoped_identifier
        for child in node.children:
            if child.type == 'scoped_identifier':
                # 获取完整导入路径
                import_path = self._node_text(child, code_bytes)
                
                # 在类映射中查找
                if import_path in self.class_map:
                    new_path = self.class_map[import_path]
                    edits.add(
                        child.start_byte,
                        child.end_byte,
                        new_path,
                        f"import: {import_path} -> {new_path}"
                    )
                    return
                
                # 也尝试匹配结尾部分（处理内部类等情况）
                for obf, orig in self.class_map.items():
                    if import_path.endswith('.' + obf.split('.')[-1]):
                        # 构建新路径
                        prefix = '.'.join(import_path.split('.')[:-1])
                        new_short = orig.split('.')[-1]
                        new_path = prefix + '.' + new_short if prefix else new_short
                        edits.add(
                            child.start_byte,
                            child.end_byte,
                            new_path,
                            f"import suffix: {import_path} -> {new_path}"
                        )
                        return
    
    # ==================== 类型推断 ====================
    
    def _resolve_expression_type(self, node, code_bytes: bytes,
                                  var_types: Dict[str, str], current_class: str) -> Optional[str]:
        """解析表达式类型"""
        if node.type == 'identifier':
            var_name = self._node_text(node, code_bytes)
            return var_types.get(var_name)
        
        elif node.type == 'this':
            return current_class
        
        elif node.type == 'field_access':
            obj_node = node.child_by_field_name('object')
            field_node = node.child_by_field_name('field')
            
            if not obj_node or not field_node:
                return None
            
            obj_type = self._resolve_expression_type(obj_node, code_bytes, var_types, current_class)
            if not obj_type:
                return None
            
            field_name = self._node_text(field_node, code_bytes)
            
            # 使用 type_index 获取字段类型
            if self.type_index:
                return self.type_index.get_field_type(obj_type, field_name)
            return None
        
        elif node.type == 'method_invocation':
            obj_node = node.child_by_field_name('object')
            name_node = node.child_by_field_name('name')
            
            if not name_node:
                return None
            
            method_name = self._node_text(name_node, code_bytes)
            
            if obj_node:
                obj_type = self._resolve_expression_type(obj_node, code_bytes, var_types, current_class)
            else:
                obj_type = current_class
            
            if obj_type and self.type_index:
                return self.type_index.get_method_return_type(obj_type, method_name)
            return None
        
        elif node.type == 'parenthesized_expression':
            for child in node.children:
                if child.type not in ('(', ')'):
                    return self._resolve_expression_type(child, code_bytes, var_types, current_class)
        
        elif node.type == 'cast_expression':
            for child in node.children:
                if child.type in ('type_identifier', 'generic_type'):
                    return self._get_type_name(child, code_bytes)
        
        return None
    
    def _resolve_method(self, obj_type: Optional[str], method_name: str, 
                        node, code_bytes: bytes) -> Optional[str]:
        """解析方法原始名称"""
        if not obj_type:
            return None
        
        # 优先使用 type_index（支持继承链）
        if self.type_index:
            args_node = node.child_by_field_name('arguments')
            arg_count = self._count_arguments(args_node) if args_node else 0
            orig = self.type_index.resolve_method(obj_type, method_name, arg_count)
            if orig:
                return orig
        
        # 回退：直接在 member_map 中查找
        if obj_type in self.member_map:
            for m in self.member_map[obj_type]:
                if m['is_method'] and m['obf'] == method_name:
                    return m['orig']
        
        return None
    
    def _resolve_field(self, obj_type: Optional[str], field_name: str) -> Optional[str]:
        """解析字段原始名称"""
        if not obj_type:
            return None
        
        # 优先使用 type_index
        if self.type_index:
            orig = self.type_index.resolve_field(obj_type, field_name)
            if orig:
                return orig
        
        # 回退
        if obj_type in self.member_map:
            for m in self.member_map[obj_type]:
                if not m['is_method'] and m['obf'] == field_name:
                    return m['orig']
        
        return None
    
    # ==================== 辅助方法 ====================
    
    def _node_text(self, node, code_bytes: bytes) -> str:
        """获取节点文本"""
        return code_bytes[node.start_byte:node.end_byte].decode('utf8')
    
    def _get_type_name(self, type_node, code_bytes: bytes) -> str:
        """从类型节点获取类型名"""
        if type_node.type == 'type_identifier':
            return self._node_text(type_node, code_bytes)
        elif type_node.type == 'generic_type':
            for child in type_node.children:
                if child.type == 'type_identifier':
                    return self._node_text(child, code_bytes)
        elif type_node.type == 'array_type':
            for child in type_node.children:
                if child.type == 'type_identifier':
                    return self._node_text(child, code_bytes) + '[]'
        
        return self._node_text(type_node, code_bytes)
    
    def _count_arguments(self, args_node) -> int:
        """计算参数数量"""
        if not args_node:
            return 0
        count = 0
        for child in args_node.children:
            if child.type not in ('(', ')', ','):
                count += 1
        return count


# ==================== 工厂函数 ====================

def create_ast_deobfuscator(class_map: Dict[str, str], member_map: Dict[str, List[dict]],
                            type_index=None) -> Optional[ASTDeobfuscator]:
    """创建 AST 反混淆器"""
    if not TREE_SITTER_AVAILABLE:
        return None
    return ASTDeobfuscator(class_map, member_map, type_index)


def is_ast_available() -> bool:
    """检查 Tree-sitter 是否可用"""
    return TREE_SITTER_AVAILABLE
