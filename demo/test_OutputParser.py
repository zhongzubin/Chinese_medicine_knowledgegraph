from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import PromptTemplate
from pydantic import BaseModel, Field

from common.llm import my_llm


# 1. 定义期望的数据结构
class Ticket(BaseModel):
    movie: str = Field(description="电影名称")
    date: str = Field(description="日期")
    count: int = Field(description="数量")

# 2. 创建解析器
parser = PydanticOutputParser(pydantic_object=Ticket)

# 3. 将格式指令注入提示词模板
prompt = PromptTemplate(
    template="... {format_instructions}",
    partial_variables={"format_instructions": parser.get_format_instructions()},
)

# 4. 调用模型获取原始输出
raw_output = my_llm.invoke(prompt)

# 5. 使用 parse 将原始文本转为 Ticket 对象
structured_ticket = parser.parse(raw_output)
# 现在可以 safe 地访问属性：structured_ticket.movie, structured_ticket.count