import os
import re
import threading

# 导入 Smali 提取器
try:
    from smali_extractor import get_smali_of_class, get_class_info
    SMALI_EXTRACTOR_AVAILABLE = True
except ImportError:
    SMALI_EXTRACTOR_AVAILABLE = False

# 导入 Tree-sitter 解析器（强制）
try:
    from ts_java_parser import GlobalTypeIndex, TreeSitterJavaParser, init_global_type_index
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False

# 强制 Tree-sitter 可用性检查
if not TREE_SITTER_AVAILABLE:
    raise ImportError("Tree-sitter 解析器不可用，请安装 tree-sitter-java 依赖: pip install tree-sitter-java")

# 导入增强模块
try:
    from deobf_enhancer import CodeEnhancer, create_enhancer, UnmappedCollector
    ENHANCER_AVAILABLE = True
except ImportError:
    ENHANCER_AVAILABLE = False

# 导入 AST-First 反混淆引擎
try:
    from ast_deobfuscator import ASTDeobfuscator, create_ast_deobfuscator, is_ast_available
    AST_DEOBFUSCATOR_AVAILABLE = is_ast_available()
except ImportError:
    AST_DEOBFUSCATOR_AVAILABLE = False

def parse_mapping(mapping_file):
    """
    解析 ProGuard 映射文件（兼容增强映射格式）。
    返回:
        class_map: {obfuscated_full_name: original_full_name}
        member_map: {obfuscated_class: [{'obf': str, 'orig': str, 'is_method': bool, 'signature': str}]}
    """
    class_map = {}
    member_map = {}
    current_obf_class = None

    with open(mapping_file, 'r', encoding='utf-8') as f:
        for line in f:
            line_raw = line
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            
            # 去除行尾注释（# 开头的部分）
            if '  #' in line:
                line = line.split('  #')[0].strip()
            
            # 类映射行：不以空格开头
            if not line_raw.startswith(' '):
                match = re.match(r'^(.*) -> (.*):$', line)
                if match:
                    obf_class = match.group(1)
                    orig_class = match.group(2)
                    class_map[obf_class] = orig_class
                    current_obf_class = obf_class
                    member_map[current_obf_class] = []
            else:
                # 成员映射行：以空格开头
                # 标准格式: [line_range:]return_type obf_name[(params)] -> orig_name
                # 增强格式: obf_name() -> orig_name (无返回类型)
                
                # 先尝试标准格式
                match = re.match(r'^(?:\d+:\d+:)?(\S+)\s+(\S+?)(\(.*?\))?\s+->\s+(\S+)$', line)
                if match and current_obf_class:
                    return_type = match.group(1)
                    obf_name = match.group(2)
                    args = match.group(3)  # "(int,int)" 或 None
                    orig_name = match.group(4)
                    member_map[current_obf_class].append({
                        'obf': obf_name,
                        'orig': orig_name,
                        'is_method': args is not None,
                        'signature': args if args else '',
                        'return_type': return_type
                    })
                else:
                    # 尝试增强格式（无返回类型）: obf_name() -> orig_name 或 obf_name -> orig_name
                    match_enhanced = re.match(r'^(\S+?)(\(\))?\s+->\s+(\S+)$', line)
                    if match_enhanced and current_obf_class:
                        obf_name = match_enhanced.group(1)
                        has_parens = match_enhanced.group(2) is not None
                        orig_name = match_enhanced.group(3)
                        member_map[current_obf_class].append({
                            'obf': obf_name,
                            'orig': orig_name,
                            'is_method': has_parens,
                            'signature': '()' if has_parens else '',
                            'return_type': ''
                        })
    return class_map, member_map


def protect_strings(content):
    """
    保护字符串字面量，避免被错误替换。
    返回: (处理后内容, 字符串字典)
    """
    string_map = {}
    counter = [0]  # 使用列表以便在闭包中修改
    
    def replace_string(m):
        key = f'__STR_{counter[0]}__'
        string_map[key] = m.group(0)
        counter[0] += 1
        return key
    
    # 匹配双引号字符串（处理转义）
    content = re.sub(r'"(?:[^"\\]|\\.)*"', replace_string, content)
    
    return content, string_map


def restore_strings(content, string_map):
    """恢复被保护的字符串字面量。"""
    for key, value in string_map.items():
        content = content.replace(key, value)
    return content


def filter_jadx_comments(content):
    """移除 JADX 警告注释，避免干扰解析。"""
    return re.sub(r'/\* JADX WARNING: .*? \*/', '', content, flags=re.DOTALL)




# ==================== Smali 回退处理 ====================

SMALI_OUTPUT_DIR = '/Users/hoto/PC_Java/smali_output'

def get_smali_fallback(obf_class_name: str) -> str:
    """
    从 smali_output 文件夹读取预生成的 smali 信息
    
    Args:
        obf_class_name: 混淆类名（点分隔）
        
    Returns:
        smali 风格的方法签名信息
    """
    smali_path = os.path.join(SMALI_OUTPUT_DIR, obf_class_name.replace('.', '/') + '.smali')
    if os.path.exists(smali_path):
        with open(smali_path, 'r', encoding='utf-8') as f:
            return f.read()
    return None


def inject_smali_for_failed_methods(content: str, obf_class_name: str) -> str:
    """
    检测 JADX 反编译失败的方法，并注入 smali 信息作为注释
    
    Args:
        content: Java 源代码内容
        obf_class_name: 当前类的混淆名
        
    Returns:
        增强后的代码内容
    """
    # 检测是否有反编译失败标记
    failure_patterns = [
        'Code decompiled incorrectly',
        'JADX WARN: Code restructure failed',
        'Method decompilation failed'
    ]
    
    has_failures = any(p in content for p in failure_patterns)
    if not has_failures:
        return content
    
    # 读取 smali 信息
    smali_info = get_smali_fallback(obf_class_name)
    if not smali_info:
        return content
    
    # 在类声明后注入 smali 信息作为注释
    smali_comment = "\n/* === SMALI METHOD SIGNATURES (for decompile-failed methods) ===\n"
    smali_comment += smali_info.replace("*/", "* /")  # 避免注释嵌套
    smali_comment += "\n=== END SMALI ===*/\n"
    
    # 在第一个 { 后插入
    match = re.search(r'((?:public|abstract|final|class|interface|enum)[^{]*\{)', content)
    if match:
        insert_pos = match.end()
        content = content[:insert_pos] + smali_comment + content[insert_pos:]
    
    return content

def deobfuscate_content(content, current_obf_full_class, class_map, member_map, sorted_obf_classes, type_index=None):
    """
    对单个代码段进行反混淆处理。
    
    Args:
        type_index: 可选的 GlobalTypeIndex 实例用于类型解析
    """
    # === 步骤 0: 保护字符串字面量 ===
    content, string_map = protect_strings(content)
    
    # PRE-SCAN: 提取导入和当前包信息
    original_imports = re.findall(r'^import\s+([\w\.]+);', content, flags=re.MULTILINE)
    current_package = '.'.join(current_obf_full_class.split('.')[:-1])
    
    # === 步骤 0.5: 标准化 JADX 匿名类名 ===
    # 处理 .AnonymousClassN 和 $AnonymousClassN 两种模式
    content = re.sub(r'\.AnonymousClass(\d+)', r'$\1', content)
    content = re.sub(r'\$AnonymousClass(\d+)', r'$\1', content)

    # === 步骤 0.6: 预保护 FQCN 中的短类名（包含无映射的类）===
    # 匹配模式: 包名.短类名( 或 包名.短类名<
    # 保护这些短类名不被后续成员替换污染
    fqcn_shortname_placeholders = {}
    fqcn_pattern = re.compile(r'(com\.corrodinggames\.[a-zA-Z0-9_.]+)\.([a-z][a-zA-Z0-9]*)(?=[(<\s])')
    
    def protect_fqcn_shortname(m):
        pkg = m.group(1)
        short = m.group(2)
        # 仅保护可能与成员冲突的短名（1-2字符）
        if len(short) <= 2:
            placeholder = f'__FQCNSHORT_{hash(pkg + short) & 0xFFFFFF:06x}__'
            fqcn_shortname_placeholders[placeholder] = short
            return f'{pkg}.{placeholder}'
        return m.group(0)
    
    content = fqcn_pattern.sub(protect_fqcn_shortname, content)

    # === 步骤 1: 全限定类名替换 (使用占位符保护避免二次污染) ===
    fqcn_placeholders = {}
    
    for i, obf in enumerate(sorted_obf_classes):
        if obf in class_map:
            obf_escaped = re.escape(obf)
            pattern = r'\b' + obf_escaped + r'\b'
            
            if obf in content: 
                placeholder = f'__FQCN_{i}__'
                content = re.sub(pattern, placeholder, content)
                fqcn_placeholders[placeholder] = class_map[obf]

    # === 保护 FQCN 占位符后的标识符 (避免 e.h 中的 h 被误伤) ===
    # 将 __FQCN_1__.h 转换为 __FQCN_1__._DOT_h
    content = re.sub(r'(__FQCN_\d+__)\.([a-zA-Z_]\w*)', r'\1.__DOT__\2', content)
    
    # === 步骤 1.5: 额外保护未被FQCN覆盖的短类名（如 new game.e 中的 e）===
    # 构建需要保护的短类名集合（与成员名冲突的类名）
    all_member_names = set()
    for members in member_map.values():
        for m in members:
            all_member_names.add(m['obf'])
    
    short_class_placeholders = {}
    for obf_full, orig_full in class_map.items():
        obf_short = obf_full.split('.')[-1]
        # 仅当短类名与某个成员名冲突时才保护
        if obf_short in all_member_names and len(obf_short) <= 2:
            # 保护 new ClassName、extends ClassName 等后的短类名不被成员替换
            placeholder = f'__SHORTCLS_{hash(obf_full) & 0xFFFFFF:06x}__'
            # 保护模式: new 包名.短类名(
            pkg_pattern = re.escape('.'.join(obf_full.split('.')[:-1])) + r'\.' + re.escape(obf_short) + r'\b'
            content = re.sub(pkg_pattern, '.'.join(obf_full.split('.')[:-1]) + '.' + placeholder, content)
            if placeholder in content:
                short_class_placeholders[placeholder] = obf_short

    # === 步骤 2: 短类名替换 ===
    local_short_map = {}
    
    # 当前类
    if current_obf_full_class in class_map:
        obf_short = current_obf_full_class.split('.')[-1]
        orig_short = class_map[current_obf_full_class].split('.')[-1]
        # 处理内部类短名
        if '$' in obf_short:
            obf_short = obf_short.split('$')[-1]
            orig_short = orig_short.split('$')[-1]
        local_short_map[obf_short] = orig_short
    
    # 导入的类
    for full_obf in original_imports:
        if full_obf in class_map:
            obf_short = full_obf.split('.')[-1]
            orig_short = class_map[full_obf].split('.')[-1]
            local_short_map[obf_short] = orig_short
            
    # 同包类（隐式导入）
    for full_obf in class_map:
        if full_obf.startswith(current_package + '.') and '.' not in full_obf[len(current_package)+1:]:
            obf_short = full_obf.split('.')[-1]
            orig_short = class_map[full_obf].split('.')[-1]
            local_short_map[obf_short] = orig_short

    # 按长度降序排列，避免子串替换问题
    short_names = sorted(local_short_map.keys(), key=len, reverse=True)
    for obf_s in short_names:
        orig_s = local_short_map[obf_s]
        if obf_s == orig_s:
            continue
        
        # 在安全的上下文中替换短类名
        pattern_pairs = [
            # 类声明
            (r'\bclass\s+' + re.escape(obf_s) + r'\b', f'class {orig_s}'),
            (r'\bextends\s+' + re.escape(obf_s) + r'\b', f'extends {orig_s}'),
            (r'\bimplements\s+' + re.escape(obf_s) + r'(?=[\s,{])', f'implements {orig_s}'),
            (r'\binterface\s+' + re.escape(obf_s) + r'\b', f'interface {orig_s}'),
            (r'\benum\s+' + re.escape(obf_s) + r'\b', f'enum {orig_s}'),
            
            # 对象创建
            (r'\bnew\s+' + re.escape(obf_s) + r'\b', f'new {orig_s}'),
            
            # 类型转换
            (r'\(\s*' + re.escape(obf_s) + r'\s*\)', f'({orig_s})'),
            
            # 访问修饰符 + 类型
            (r'\bpublic\s+' + re.escape(obf_s) + r'\b', f'public {orig_s}'),
            (r'\bprivate\s+' + re.escape(obf_s) + r'\b', f'private {orig_s}'),
            (r'\bprotected\s+' + re.escape(obf_s) + r'\b', f'protected {orig_s}'),
            (r'\bstatic\s+' + re.escape(obf_s) + r'\b', f'static {orig_s}'),
            (r'\bfinal\s+' + re.escape(obf_s) + r'\b', f'final {orig_s}'),
            (r'\babstract\s+' + re.escape(obf_s) + r'\b', f'abstract {orig_s}'),
            
            # 泛型
            (r'<' + re.escape(obf_s) + r'>', f'<{orig_s}>'),
            (r'<' + re.escape(obf_s) + r',', f'<{orig_s},'),
            (r',\s*' + re.escape(obf_s) + r'>', f', {orig_s}>'),
            (r'<\?\s+extends\s+' + re.escape(obf_s) + r'\b', f'<? extends {orig_s}'),
            (r'<\?\s+super\s+' + re.escape(obf_s) + r'\b', f'<? super {orig_s}'),
            
            # .class 访问
            (r'\b' + re.escape(obf_s) + r'\.class\b', f'{orig_s}.class'),
            
            # instanceof
            (r'\binstanceof\s+' + re.escape(obf_s) + r'\b', f'instanceof {orig_s}'),
            
            # 变量声明 (Type varName)
            (r'\b' + re.escape(obf_s) + r'\b(?=\s+\w+\s*[;=,\)])', f'{orig_s}'),
            
            # 数组类型
            (r'\b' + re.escape(obf_s) + r'\b(?=\s*\[\s*\])', f'{orig_s}'),
            
            # 方法返回类型
            (r'(?<=\s)' + re.escape(obf_s) + r'\b(?=\s+\w+\s*\()', f'{orig_s}'),
            
            # 参数类型
            (r'(?<=\()\s*' + re.escape(obf_s) + r'\b(?=\s+\w)', f'{orig_s}'),
            (r'(?<=,)\s*' + re.escape(obf_s) + r'\b(?=\s+\w)', f' {orig_s}'),
        ]
        
        for p, r_val in pattern_pairs:
            content = re.sub(p, r_val, content)

    # === 步骤 2.5: 保护已替换的短类名避免后续被成员替换污染 ===
    class_name_placeholders = {}
    for obf_s, orig_s in local_short_map.items():
        if obf_s != orig_s and len(orig_s) >= 2:  # 仅保护有意义的类名
            placeholder = f'__CLASSNAME_{hash(orig_s) & 0xFFFFFF:06x}__'
            # 仅在类型上下文中保护（避免保护字符串中的类名）
            # 保护模式: 点号后的类名（如 game.PlayerTeam）
            content = re.sub(r'\.' + re.escape(orig_s) + r'\b', '.' + placeholder, content)
            # 保护模式: new 后的类名
            content = re.sub(r'\bnew\s+' + re.escape(orig_s) + r'\b', 'new ' + placeholder, content)
            # 保护模式: 类声明中的类名
            content = re.sub(r'\bclass\s+' + re.escape(orig_s) + r'\b', 'class ' + placeholder, content)
            # 保护模式: extends/implements 后的类名
            content = re.sub(r'\bextends\s+' + re.escape(orig_s) + r'\b', 'extends ' + placeholder, content)
            content = re.sub(r'\bimplements\s+' + re.escape(orig_s) + r'\b', 'implements ' + placeholder, content)
            if placeholder in content:
                class_name_placeholders[placeholder] = orig_s

    # === 步骤 3: 成员替换（区分字段和方法）===
    # 注：高级类型处理已由 AST 引擎在上游完成，此处仅作回退
    
    if current_obf_full_class in member_map:
        members = member_map[current_obf_full_class]
        
        # 分离方法和字段
        methods = [m for m in members if m['is_method']]
        fields = [m for m in members if not m['is_method']]
        
        # 方法去重：回退模式下只保留第一个
        if type_index is None:
            seen_method_obf = set()
            unique_methods = []
            for m in methods:
                if m['obf'] not in seen_method_obf:
                    seen_method_obf.add(m['obf'])
                    unique_methods.append(m)
            methods = unique_methods
        
        # 方法替换：仅在后跟 '(' 的上下文中替换
        methods.sort(key=lambda x: len(x['obf']), reverse=True)
        for m in methods:
            obf_esc = re.escape(m['obf'])
            # 方法调用模式：标识符后紧跟括号
            # 避免替换类声明上下文
            pattern = r'(?<![.\w])' + obf_esc + r'(?=\s*\()'
            content = re.sub(pattern, m['orig'], content)
        
        # 字段替换：排除方法调用上下文
        fields.sort(key=lambda x: len(x['obf']), reverse=True)
        for m in fields:
            obf_esc = re.escape(m['obf'])
            
            # 模式 1: this.field 或 obj.field (点号后)
            pattern1 = r'(?<=\.)' + obf_esc + r'\b(?!\s*\()'
            content = re.sub(pattern1, m['orig'], content)
            
            # 模式 2: 字段赋值/使用
            # 排除类声明关键字后的位置
            protected_prefixes = (
                r'(?<!\bclass\s)'
                r'(?<!\bextends\s)'
                r'(?<!\bimplements\s)'
                r'(?<!\bnew\s)'
                r'(?<!\bpublic\s)'
                r'(?<!\bprivate\s)'
                r'(?<!\bprotected\s)'
                r'(?<!\binterface\s)'
                r'(?<!\benum\s)'
                r'(?<!\bstatic\s)'
                r'(?<!\bfinal\s)'
                r'(?<!\bvoid\s)'
                r'(?<!\bint\s)'
                r'(?<!\blong\s)'
                r'(?<!\bfloat\s)'
                r'(?<!\bdouble\s)'
                r'(?<!\bboolean\s)'
                r'(?<!\bbyte\s)'
                r'(?<!\bchar\s)'
                r'(?<!\bshort\s)'
            )
            
            # 独立字段访问（在赋值、分号、逗号等结束符前）
            pattern2 = protected_prefixes + r'\b' + obf_esc + r'\b(?=\s*[=;,\)\]])'
            content = re.sub(pattern2, m['orig'], content)
            
            # 数组索引访问
            pattern3 = protected_prefixes + r'\b' + obf_esc + r'\b(?=\s*\[)'
            content = re.sub(pattern3, m['orig'], content)

    # === 步骤 3 已移除: 原兜底正则替换逻辑过于激进 ===
    # Tree-sitter 解析器已处理绝大多数情况，此兜底逻辑导致：
    # 1. 同一标识符被多轮替换导致粘连 (如 teamStatisticseamStatistics)
    # 2. 局部变量/参数被错误替换为成员名
    # 3. 跨类成员访问被当前类映射污染
    # === 步骤 3.5: 接口/抽象方法定义回退替换 ===
    # 基于签名（返回类型）进行更精准的回退匹配
    if type_index:
        index = type_index
        # 提取方法声明模式: 返回类型 方法名()
        # 注意: 排除 FQCN 中的短类名（如 game.e(...)中的e是类名不是方法名）
        # 方法声明必须有 "类型 空格 方法名" 的形式,且方法名前不能是点号
        method_decl_pattern = re.compile(r'\b(int|boolean|void|float|double|long|short|byte|char|[A-Z][a-zA-Z0-9_]*)\s+([a-z][a-zA-Z0-9]?)\s*\(')
        
        # 收集所有匹配并从后往前替换（避免索引偏移）
        matches = list(method_decl_pattern.finditer(content))
        for match in reversed(matches):
            ret_type = match.group(1)
            obf_m = match.group(2)
            
            # 跳过 FQCN 中的短类名（返回类型包含点号说明是包名而非简单类型）
            if '.' in ret_type:
                continue
            
            # 优先使用基于签名的精确回退
            sig_key = (obf_m, ret_type)
            if hasattr(index, 'method_by_signature') and sig_key in index.method_by_signature:
                fallback_name = index.method_by_signature[sig_key]
            elif obf_m in index.global_method_fallback:
                fallback_name = index.global_method_fallback[obf_m]
            else:
                continue
            
            # 仅替换此特定位置
            start, end = match.start(2), match.end(2)
            content = content[:start] + fallback_name + content[end:]

    # === 步骤 4: 修复 package 声明 ===
    if current_obf_full_class in class_map:
        new_full_name = class_map[current_obf_full_class]
        new_package = '.'.join(new_full_name.split('.')[:-1])
        content = re.sub(r'^package\s+[\w\.]+;', f'package {new_package};', content, flags=re.MULTILINE)
    
    # === 步骤 4.5: 恢复全限定类名占位符 ===
    # 先恢复内部保护的点
    content = content.replace('.__DOT__', '.')
    for placeholder, original in fqcn_placeholders.items():
        content = content.replace(placeholder, original)
    
    # 恢复步骤 2.5 中保护的短类名
    for placeholder, orig_class in class_name_placeholders.items():
        content = content.replace(placeholder, orig_class)
    
    # 恢复步骤 1.5 中保护的短类名（冲突保护）
    for placeholder, orig_short in short_class_placeholders.items():
        content = content.replace(placeholder, orig_short)
    
    # 恢复步骤 0.6 中预保护的 FQCN 短类名
    for placeholder, orig_short in fqcn_shortname_placeholders.items():
        content = content.replace(placeholder, orig_short)

    # === 步骤 5: 恢复字符串字面量 ===
    content = restore_strings(content, string_map)
    
    # === 步骤 5.5: 反射字符串处理 ===
    # 1. Class.forName("obf.class.name") -> Class.forName("orig.class.name")
    def replace_forname_class(m):
        class_str = m.group(1)
        if class_str in class_map:
            return f'Class.forName("{class_map[class_str]}")'
        return m.group(0)
    
    content = re.sub(
        r'Class\.forName\s*\(\s*"([^"]+)"\s*\)',
        replace_forname_class,
        content
    )
    
    # 2. getMethod/getDeclaredMethod("methodName") -> ("origMethodName")
    def replace_method_string(m):
        call_type = m.group(1)
        method_str = m.group(2)
        # 在当前类的成员映射中查找
        if current_obf_full_class in member_map:
            for member in member_map[current_obf_full_class]:
                if member['is_method'] and member['obf'] == method_str:
                    return f'{call_type}("{member["orig"]}"'
        # 全局回退：查找所有类的方法映射
        for obf_class, members in member_map.items():
            for member in members:
                if member['is_method'] and member['obf'] == method_str:
                    return f'{call_type}("{member["orig"]}"'
        return m.group(0)
    
    content = re.sub(
        r'(getMethod|getDeclaredMethod)\s*\(\s*"(\w+)"',
        replace_method_string,
        content
    )
    
    # === 步骤 6: Smali 回退处理（针对反编译失败的方法）===
    content = inject_smali_for_failed_methods(content, current_obf_full_class)
    
    return content


def process_merged_files(input_root, output_root, class_map, member_map, use_advanced=True, java_files_to_process=None):
    """
    处理所有文件。
    """
    sorted_obf_classes = sorted(class_map.keys(), key=len, reverse=True)
    
    # 创建类型索引和 AST 引擎
    type_index = None
    ast_deobfuscator = None
    
    # 使用 AST-First 引擎（强制 Tree-sitter）
    if use_advanced and AST_DEOBFUSCATOR_AVAILABLE:
        type_index = init_global_type_index(class_map, member_map)
        ast_deobfuscator = create_ast_deobfuscator(class_map, member_map, type_index)
        print("  - 使用 AST-First 反混淆引擎")
    elif use_advanced:
        print("  - 警告: AST 引擎不可用，使用基础替换")

    if ENHANCER_AVAILABLE:
        enhancer = create_enhancer(class_map, member_map)
        unmapped_collector = UnmappedCollector()
        print("  - 启用启发式命名增强")
    
    # 统计计数器
    stats = {
        'ast_success': 0,
        'ast_fallback': 0,
        'fallback_reasons': {}
    }
    
    # === 预扫描阶段: 构建继承树 ===
    if type_index:
        print("正在进行全量预扫描以构建继承关系索引...")
        scanner = TreeSitterJavaParser()
        for root, dirs, files in os.walk(input_root):
            for file in files:
                if (file.endswith('.txt') or file.endswith('.java')) and not file.startswith('.'):
                    file_path = os.path.join(root, file)
                    with open(file_path, 'r', encoding='utf-8') as f:
                        text = f.read()
                    
                    segments = []
                    if file.endswith('.txt'):
                        pts = re.split(r'(=+\n// FILE_PATH: (.+?)\n=+)', text)
                        segments = [pts[i] for i in range(3, len(pts), 3)]
                    else:
                        segments = [text]
                    
                    for seg in segments:
                        try:
                            # 预处理：过滤注释
                            seg_clean = filter_jadx_comments(seg)
                            info = scanner.extract_type_info(seg_clean)
                            if info.class_name:
                                if info.parent_class:
                                    type_index.set_inheritance(info.class_name, info.parent_class)
                                # 记录所有接口继承关系
                                for itf in info.interfaces:
                                    type_index.set_inheritance(info.class_name, itf)
                        except:
                            continue
        print("  - 继承索引构建完成")

    processed_count = 0
    
    for root, dirs, files in os.walk(input_root):
        for file in files:
            file_abs_path = os.path.join(root, file)
            # 调试限制
            if java_files_to_process and file_abs_path not in java_files_to_process:
                continue
                
            is_merged = file.endswith('.txt')
            is_java = file.endswith('.java')
            
            if (is_merged or is_java) and not file.startswith('.'):
                file_path = os.path.join(root, file)
                rel_dir = os.path.relpath(root, input_root)
                
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read()

                if is_merged:
                    delimiter_pattern = r'(=+\n// FILE_PATH: (.+?)\n=+)'
                    parts = re.split(delimiter_pattern, content)
                else:
                    # 对于单个 .java 文件，构造一个伪零件列表以复用逻辑
                    rel_path = os.path.relpath(file_path, input_root)
                    parts = ["", "", rel_path, content]
                
                processed_content = parts[0]
                
                for i in range(1, len(parts), 3):
                    delimiter_full = parts[i]
                    obf_rel_path = parts[i+1]
                    code_segment = parts[i+2]
                    
                    # 预处理：过滤 JADX 注释
                    code_segment = filter_jadx_comments(code_segment)
                    
                    tmp_path = obf_rel_path
                    if tmp_path.startswith('./'):
                        tmp_path = tmp_path[2:]
                    current_obf_full_class = tmp_path.replace('/', '.').replace('.java', '')
                    
                    # 匹配混淆类
                    matched_obf_class = None
                    if current_obf_full_class in class_map:
                        matched_obf_class = current_obf_full_class
                    else:
                        for obf_name in sorted_obf_classes:
                            if obf_name.endswith(current_obf_full_class):
                                matched_obf_class = obf_name
                                break
                    
                    if not matched_obf_class:
                        matched_obf_class = current_obf_full_class

                    # === AST-First 处理流程 ===
                    processed_segment = None
                    
                    if ast_deobfuscator:
                        try:
                            # 使用 AST 引擎直接处理原始代码（无预处理）
                            processed_segment = ast_deobfuscator.process(code_segment, matched_obf_class)
                            
                            # 补充处理：package 声明修复
                            if matched_obf_class in class_map:
                                new_full_name = class_map[matched_obf_class]
                                new_package = '.'.join(new_full_name.split('.')[:-1])
                                processed_segment = re.sub(
                                    r'^package\s+[\w\.]+;', 
                                    f'package {new_package};', 
                                    processed_segment, 
                                    flags=re.MULTILINE
                                )
                            stats['ast_success'] += 1
                        except Exception as e:
                            # AST 解析失败，回退到正则处理
                            processed_segment = None
                            stats['ast_fallback'] += 1
                            reason = str(e)[:50]
                            stats['fallback_reasons'][reason] = stats['fallback_reasons'].get(reason, 0) + 1
                    
                    # 回退：使用正则处理流程
                    if processed_segment is None:
                        processed_segment = deobfuscate_content(
                            code_segment, matched_obf_class, class_map, member_map, 
                            sorted_obf_classes, type_index
                        )
                    
                    # 应用启发式命名增强
                    if enhancer:
                        processed_segment = enhancer.enhance(processed_segment, matched_obf_class)
                    
                    if matched_obf_class in class_map:
                        new_rel_path = class_map[matched_obf_class].replace('.', '/') + '.java'
                    else:
                        new_rel_path = obf_rel_path
                    
                    new_delimiter = delimiter_full.replace(obf_rel_path, new_rel_path)
                    processed_content += new_delimiter + processed_segment

                # 决定输出文件名和路径
                if is_merged:
                    output_file_name = file.replace('.txt', '_processed.txt')
                else:
                    if matched_obf_class in class_map:
                        output_file_name = class_map[matched_obf_class].split('.')[-1] + '.java'
                    else:
                        output_file_name = file
                
                output_file_path = os.path.join(output_root, rel_dir, output_file_name)
                os.makedirs(os.path.dirname(output_file_path), exist_ok=True)
                
                with open(output_file_path, 'w', encoding='utf-8') as f:
                    f.write(processed_content)
                
                processed_count += 1
    
    # 输出 AST 处理统计
    if ast_deobfuscator and (stats['ast_success'] > 0 or stats['ast_fallback'] > 0):
        total = stats['ast_success'] + stats['ast_fallback']
        success_rate = stats['ast_success'] / total * 100 if total > 0 else 0
        print(f"\n=== AST 处理统计 ===")
        print(f"  - AST 成功: {stats['ast_success']} ({success_rate:.1f}%)")
        print(f"  - 回退正则: {stats['ast_fallback']} ({100 - success_rate:.1f}%)")
        if stats['fallback_reasons']:
            print(f"  - 回退原因:")
            for reason, count in sorted(stats['fallback_reasons'].items(), key=lambda x: -x[1])[:5]:
                print(f"      {reason}: {count}")
    
    return processed_count


if __name__ == '__main__':
    mapping_file = '/Users/hoto/PC_Java/mappings.txt'
    input_root = '/Users/hoto/PC_Java/jadx_output/sources'
    output_root = '/Users/hoto/PC_Java/processed_output'
    
    print("=== 反混淆处理脚本 (优化版) ===")
    print(f"映射文件: {mapping_file}")
    print(f"输入目录: {input_root}")
    print(f"输出目录: {output_root}")
    print()
    
    print("解析映射文件...")
    class_map, member_map = parse_mapping(mapping_file)
    print(f"  - 类映射数: {len(class_map)}")
    print(f"  - 成员映射类数: {len(member_map)}")
    
    total_members = sum(len(v) for v in member_map.values())
    print(f"  - 成员映射总数: {total_members}")
    print()
    
    # 获取所有待处理的 .java 文件
    all_files = []
    for root, dirs, files in os.walk(input_root):
        for file in files:
            if file.endswith('.java'):
                all_files.append(os.path.join(root, file))
    
    print(f"  - 待处理 Java 文件数: {len(all_files)}")
    
    print("处理代码文件...")
    count = process_merged_files(input_root, output_root, class_map, member_map)
    print(f"  - 处理文件数: {count}")
    print()
    
    print("完成!")
