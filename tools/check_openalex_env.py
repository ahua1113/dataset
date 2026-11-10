import os

api_key = os.getenv("OPENALEX_API_KEY")

if api_key:
    print("OPENALEX_API_KEY 已配置")
else:
    print("OPENALEX_API_KEY 未配置")