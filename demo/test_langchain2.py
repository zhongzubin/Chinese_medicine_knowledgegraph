# 1. 导入组件
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

# 2. 用 BaseModel 定义期望的输出结构
class TranslationResult(BaseModel):
    """翻译结果的期望结构"""
    translated_text: str = Field(description="翻译后的目标语言文本")
    source_lang: str = Field(description="检测到的源语言，如 中文、英文")
    target_lang: str = Field(description="目标语言")
    confidence: float = Field(description="翻译置信度，0 到 1 之间的小数")
    notes: str = Field(default="", description="翻译备注，如无则为空字符串")

# 3. 创建解析器（会自动生成 JSON Schema 格式说明）
parser = PydanticOutputParser(pydantic_object=TranslationResult)

# 4. 定义提示词模板，把格式说明注入进去
prompt_template = ChatPromptTemplate.from_messages([
    ("system",
     "你是一个专业的翻译官。请将用户输入的文本从 {source_lang} 翻译成 {target_lang}，"
     "并严格按照以下格式返回结果。\n\n{format_instructions}"),
    ("human", "{text}")
])

# 5. 初始化模型
model = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)

# 6. 用管道符串联：提示词 → 模型 → 解析器
chain = prompt_template | model | parser

# 7. 调用链，注意要用 partial_variables 注入 format_instructions
result = chain.invoke({
    "source_lang": "中文",
    "target_lang": "英文",
    "text": "你好，今天天气不错。",
    "format_instructions": parser.get_format_instructions(),  # ← 注入格式要求
})

# 8. result 已经是 TranslationResult 对象，可以直接访问属性
print(type(result))                 # <class '__main__.TranslationResult'>
print(result.translated_text)       # Hello, the weather is nice today.
print(result.source_lang)           # 中文
print(result.confidence)            # 0.98
print(result.notes)                 # （可能为空）
print(result.model_dump())          # 转成 dict，方便存库或下游使用