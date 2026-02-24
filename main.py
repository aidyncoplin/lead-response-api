import os
import csv
from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables
load_dotenv()

# Create OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def generate_followup(name, service, interest):
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "You write short, professional follow-up messages for small businesses.",
            },
            {
                "role": "user",
                "content": f"Name: {name}\nService: {service}\nInterest: {interest}\nWrite a 2-sentence text message follow-up.",
            },
        ],
    )
    return response.choices[0].message.content


def save_to_csv(name, service, interest, message):
    file_exists = os.path.isfile("leads.csv")

    with open("leads.csv", mode="a", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)

        if not file_exists:
            writer.writerow(["Name", "Service", "Interest", "AI_Response"])

        writer.writerow([name, service, interest, message])


# Test Leads
leads = [
    ("John", "Roofing estimate", "Leak repair"),
    ("Sarah", "Kitchen remodel", "Cabinet upgrade"),
]

for name, service, interest in leads:
    msg = generate_followup(name, service, interest)
    save_to_csv(name, service, interest, msg)
    print(f"Saved lead for {name}")