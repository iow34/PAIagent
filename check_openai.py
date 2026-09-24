from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

client = OpenAI()

models = client.models.list()
print("З'єднання з OpenAI API успішне. Перші доступні моделі:")
for model in list(models)[:50]:
    print(" -", model.id)