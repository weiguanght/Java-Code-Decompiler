# 铁锈战争Java源码反混淆

此项目是一个针对 JADX 反编译输出的高级后处理工具集。通过结合 ProGuard 映射文件、Smali 字节码分析、AST（抽象语法树）解析以及启发式算法，实现高精度的代码还原、重构和增强。

核心目标是提高反编译代码的可读性。

## 项目概述

该系统不仅仅是简单的字符串替换工具，它构建了一个多层级的分析管道：

1. **字节码层**：通过 `javap` 和 Smali 分析提取精确的方法签名和类层级关系。
2. **语法层**：使用 `tree-sitter` 构建 Java AST，进行上下文感知的类型推断和重构，避免正则替换的副作用。
3. **语义层**：通过交叉引用（XRef）分析、Android 接口映射和字符串常量追踪，推断混淆方法的真实含义。

## 模块功能详解

### 1. 核心反混淆引擎

- **`process_java.py`**: 主入口脚本。协调各个子模块，读取混淆代码和映射文件，执行反混淆流程。支持 JADX 导出的目录或合并的文本文件。
- **`ast_deobfuscator.py`**: 基于 AST 的反混淆引擎。利用 `tree-sitter` 解析代码结构，精确通过节点位置进行标识符替换，彻底解决正则替换导致的误伤（如字符串内替换、局部变量与成员变量冲突）。
- **`ts_java_parser.py`**: 封装 `tree-sitter-java` 的高级解析器。提供容错解析、跨类成员解析、继承链分析和类型追踪功能。
- **`java_parser.py`**: 基于正则的备用解析器。在 AST 解析不可用时的回退方案，提供基础的类型感知替换。

### 2. 字节码与 Smali 分析

- **`jar_bytecode_extractor.py`**: 从 JAR 文件中提取完整字节码，生成类 Smali 格式的中间文件，用于获取真实的指令流。
- **`smali_extractor.py`**: 利用 `javap` 批量提取类的 Smali 风格接口定义（方法签名、字段类型），用于构建类型数据库。
- **`xref_analyzer.py`**: 交叉引用分析器。构建全局方法调用图（Call Graph）和字段访问索引，用于从调用上下文推断方法语义（例如：被 `onDraw` 调用的方法可能与绘制相关）。
- **`smali_enhanced_deobf.py`**: 桥接模块。将 Smali 分析结果应用于 Java 源代码，修复反编译丢失的泛型信息或修正错误的方法签名。

### 3. 映射增强与推断

- **`mapping_enhancer.py`**: 映射生成器。结合 Smali 分析、DeepEnhancer 和启发式规则，自动推断未映射成员的名称，生成新的 `mappings_enhanced.txt`。
- **`enhanced_deobf.py`**: 代码增强主脚本 (v3.0)。集成深度增强器，在代码中注入详细的注释（如 `@SmaliSig`, `@FieldInferred`），并利用字符串常量（如 `getString("player_name")`）推断字段名。
- **`deobf_enhancer.py`**: 启发式变量重命名工具。自动识别循环变量（`i`, `j`）、异常变量（`ex`）以及 getter/setter 模式，优化局部变量命名。
- **`android_interface_mapper.py`**: Android SDK 专用映射器。自动识别实现标准接口（如 `OnClickListener`）的混淆类，并将混淆方法（如 `a(View v)`）还原为标准名称（`onClick`）。

### 4. 辅助工具

- **`native_mapper.py`**: JNI 分析工具。扫描 `native` 方法并生成符合 JNI 规范的 C/C++ 函数名映射，辅助底层逆向。
- **`fix_structure.py`**: 目录重构工具。根据 Java 文件中的 `package` 声明，将文件移动到正确的目录层级中。

## 依赖环境

- **Python 3.8+**
- **JDK** (需在 PATH 中包含 `javap` 命令)
- **Python 库**:
    - `tree-sitter`
    - `tree-sitter-java`

## 工作流逻辑

1. **准备阶段**:
    - 使用 `jar_bytecode_extractor.py` 或 `smali_extractor.py` 处理原始 JAR，生成字节码索引。
    - 准备 JADX 反编译出的源代码和 ProGuard 映射文件 (`mappings.txt`)。
2. **映射增强 (可选)**:
    - 运行 `mapping_enhancer.py`。它会利用字节码信息和启发式算法分析代码，扩充原始的映射文件。
3. **执行反混淆**:
    - 运行 `process_java.py`。
    - 脚本优先加载 `ts_java_parser` 进行 AST 解析。
    - 加载 `mappings.txt` 和字节码索引。
    - 对代码进行全量反混淆，包括类名、方法名、字段名重构。
4. **代码增强**:
    - 运行 `enhanced_deobf.py`。
    - 基于调用图 (`xref_analyzer`) 和字符串追踪 (`StringTracer`)，在代码中注入语义推断注释和类型提示。
    - 利用 `android_interface_mapper` 修复 Android 回调方法名。
5. **重构整理**:
    - 运行 `fix_structure.py` 将处理后的文件按包名结构整理到文件夹中。

## 核心优势

- **AST-First**: 摒弃了传统的纯正则替换方案，解决了"短类名替换污染"（如类名 `a` 误替换变量 `a`）和"上下文丢失"问题。
- **继承链感知**: 在解析方法调用时，能够正确处理多态和继承关系，即使方法定义在父类中也能正确映射。
- **深度语义推断**: 能够通过代码中的字符串常量（如 JSON key、Preferences key）反向推断对应的字段名称。
- **JNI 支持**: 自动生成 Native 方法签名，便于动态调试和 Hook。

# 详细使用说明

以下为Java 反混淆与深度静态分析脚本集的详细使用指南。

## 1. 环境准备

在运行脚本之前，请确保环境满足以下要求：

- **Python 环境**: Python 3.8 或更高版本。
- **Java 环境**: 需安装 JDK，并确保 `javap` 命令在系统 PATH 中可用（用于字节码提取）。
- **依赖库安装**: 需要安装 `tree-sitter` 和 `tree-sitter-java` 以支持 AST 解析。Bash
    
    `pip install tree-sitter tree-sitter-java`
    

## 2. 标准工作流

建议按照以下顺序执行脚本，以获得最佳的反混淆效果。

### 步骤 0: 数据准备

1. 使用 **JADX** 将目标 APK/JAR 反编译为 Java 源代码（确保保存为 `.java` 文件结构或合并的文本文件）。
2. 获取对应的 ProGuard `mappings.txt` 文件。
3. 保留原始 JAR 文件（用于字节码分析）。

### 步骤 1: 提取字节码信息 (Smali Analysis)

从原始 JAR 中提取类结构信息，建立类型数据库。

- **脚本**: `smali_extractor.py` 或 `jar_bytecode_extractor.py`
- **用法**:Bash
    
    `# 语法: python3 smali_extractor.py [JAR路径] [输出目录]
    python3 smali_extractor.pylibs/game-lib.jar smali_output`
    
    *输出*: 在 `smali_output` 目录下生成对应的 `.smali` 签名文件。
    

### 步骤 2: 交叉引用分析 (可选，推荐)

构建全局调用图，辅助语义推断。

- **脚本**: `xref_analyzer.py`
- **用法**:Bash
    
    `python3 xref_analyzer.py --smali-dir smali_output --workers 8`
    

### 步骤 3: 映射文件增强

利用字节码分析结果和启发式算法，自动推断未映射方法的名称，生成增强版映射表。

- **脚本**: `mapping_enhancer.py`
- **用法**:Bash
    
    `python3 mapping_enhancer.py \
        --mapping-file mappings.txt \
        --smali-dir smali_output \
        --output mappings_enhanced.txt`
    

### 步骤 4: 执行核心反混淆

使用 AST 引擎对源代码进行重构和重命名。

- **脚本**: `process_java.py`
- **注意**: 此脚本目前主要通过修改源码中的配置变量运行。
- **配置**: 打开 `process_java.py`，修改底部的 `main` 块：Python
    
    `if __name__ == '__main__':
        mapping_file = '/path/to/mappings_enhanced.txt'  # 使用步骤3生成的增强映射
        input_root = '/path/to/jadx_output/sources'      # JADX 导出的源码目录
        output_root = '/path/to/processed_output'        # 输出目录
        # ...`
    
- **运行**:Bash
    
    `python3 process_java.py`
    

### 步骤 5: 代码深度增强与注释

在已反混淆（或原始）代码中注入类型签名、继承关系和推断出的字段含义注释。

- **脚本**: `enhanced_deobf.py`
- **用法**:Bash
    
    `python3 enhanced_deobf.py \
        --mapping-file mappings_enhanced.txt \
        --input-dir processed_output \
        --output-dir final_output \
        --smali-dir smali_output \
        --enable-xref`
    
    *参数说明*:
    
    - `-enable-xref`: 启用基于调用图的深度语义推断（需先完成步骤 2）。

### 步骤 6: 目录结构重构

根据 Java 文件中的 `package` 声明，将文件移动到正确的目录层级。

- **脚本**: `fix_structure.py`
- **配置**: 打开脚本，修改 `TARGET_DIR` 变量：Python
    
    `TARGET_DIR = "final_output" # 指向步骤5的输出目录`
    
- **运行**:Bash
    
    `python3 fix_structure.py`
    

## 3. 辅助工具使用

### Native 方法映射生成 (JNI)

生成用于底层逆向的 JNI 函数名映射表。

- **脚本**: `native_mapper.py`
- **配置**: 修改脚本中的 `source_dir` (源码目录) 和 `output_path`。
- **运行**:Bash
    
    `python3 native_mapper.py`
    
- **输出**: 生成 `native_mapping.txt` 和 `native_mapping.json`。

## 4. 常见问题排查

1. **AST 解析失败**:
    - 如果是 `process_java.py` 报错，请检查是否安装了 `tree-sitter` 库。
    - 脚本内置了回退机制，AST 失败时会自动降级为正则模式 (`java_parser.py`)，但精度会下降。
2. **路径错误**:
    - 部分脚本（如 `process_java.py`, `fix_structure.py`, `native_mapper.py`）未完全实现命令行参数解析，**必须**手动修改脚本内的路径变量。
3. **编码问题**:
    - 所有文件读写均强制使用 `utf-8`。如果遇到编码错误，请检查输入文件（JADX 导出文件）的编码格式。