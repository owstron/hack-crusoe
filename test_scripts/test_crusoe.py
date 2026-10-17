from openai import OpenAI
import os

crusoe_api_key = os.getenv("CRUSOE_API_KEY")
crusoe_url = os.getenv("CRUSOE_URL")
ydc_api_key = os.getenv("YDC_API_KEY")

client = OpenAI(
    base_url=crusoe_url,
    api_key=crusoe_api_key,
)

response = client.chat.completions.create(
    model='deepseek-ai/Deepseek-V4-Flash',
    messages=[
        {
            'role': 'user',
            'content': 'Hello, how are you?'
        }
    ],
)

print(response.to_json())