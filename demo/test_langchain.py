# 1. 导入必要的组件
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import StrOutputParser

# 2. 定义提示词模板
# 使用系统消息设定角色，用户消息承载具体变量
prompt_template = ChatPromptTemplate.from_messages([
    ("system", "你是一个专业的翻译官，请将用户输入的文本从 {source_lang} 翻译成 {target_lang}。"),
    ("human", "{text}")
])

# 3. 初始化模型（需替换为你的API Key，或配置环境变量 OPENAI_API_KEY）
model = ChatOpenAI(model="gpt-3.5-turbo", temperature=0)

# 4. 定义输出解析器，将模型返回的AIMessage对象直接转为纯字符串
parser = StrOutputParser()

# 5. 使用管道符 `|` 将三个组件串联成一条链 (LCEL)
chain = prompt_template | model | parser

# 6. 调用链，使用 .invoke() 方法并传入包含所有变量的字典
response = chain.invoke({
    "source_lang": "中文",
    "target_lang": "英文",
    "text": "你好，今天天气不错。"
})

print(response)
# 预期输出: Hello, the weather is nice today.