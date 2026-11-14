from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI()

models = client.models.list()
print("Connection to the OpenAI API successful. First available models:")
for model in list(models)[:50]:
    print(" -", model.id)
