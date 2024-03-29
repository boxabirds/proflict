from openai import OpenAI
import instructor
from pydantic import BaseModel, ValidationError
import argparse
import os
import random
import time
import csv
import re 
import json
from json import JSONDecodeError
import pandas as pd
from pathlib import Path



# Constants defining categories for message types and configurations for the API.
CATEGORIES = ["Disrespect", "Dishonesty", "Negativity", "Hostility"]
LOCAL_API_URL = 'http://gruntus:11434/v1'
LOCAL_API_KEY = 'ollama'
LOCAL_MODEL = 'mistral:7b'
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")  # Fetch the OpenAI API key from environment variables.
OPENAI_MODEL = "gpt-3.5-turbo"  # Model identifier for OpenAI's API.

class MessagePair(BaseModel):
    """
    Represents a pair of messages, one being respectful and the other non-disrespectful.

    Attributes:
        respectful (str): A respectful or neutral message.
        nondisrespectful (str): A corresponding disrespectful message.
    """
    disrespectful: str
    nondisrespectful: str

class CategorisedMessages(BaseModel):
    """
    Holds a list of MessagePair objects, each representing a pair of messages with varying respectfulness.

    Attributes:
        messages (List[MessagePair]): A list of message pairs categorized by their tone.
    """
    messages: list[MessagePair]

def strip_code_block(content):
    pattern = r'^\s*```json\s*(.*?)\s*```\s*$'
    match = re.match(pattern, content, re.DOTALL)
    if match:
        print("stripping code block")
        # Extract the JSON content without the code block syntax
        content = match.group(1)
    else:
        print("not stripping code block")
    return content

def dedupe_messages(messages_file_name:str):
    """
    despite our best intentions, there might be some duplicated messages, so we need to deduplicate them.
    """
    input_path = Path(messages_file_name)
    df = pd.read_csv(input_path)  # Replace 'your_file.csv' with the path to your CSV file

    # Step 3: Drop duplicates based on the 'nondisrespectful' column
    deduplicated_df = df.drop_duplicates(subset=['nondisrespectful'], keep='first')

    output_filename = input_path.stem + '-deduped' + input_path.suffix
    # Step 4: Save the deduplicated DataFrame to a new CSV file
    deduplicated_df.to_csv(output_filename, index=False)  # This saves the deduplicated DataFrame without the index


def create_binary_classification_dataset(messages_file_name:str):
    input_path = Path(messages_file_name)
    df = pd.read_csv(input_path)

    # Initialize an empty list for the transformed data
    transformed_data = []

    for _, row in df.iterrows():
        # Respectful message (label it as False for 'is_respectful')
        transformed_data.append({'is_respectful': False, 'message': row['nondisrespectful']})
        
        # Disrespectful message (label it as True for 'is_respectful')
        transformed_data.append({'is_respectful': True, 'message': row['disrespectful']})

    # Create a DataFrame from the transformed data
    transformed_df = pd.DataFrame(transformed_data, columns=['is_respectful', 'message'])

    # Define the output file name based on the source file name
    output_file = input_path.stem + '-binary-classifier' + input_path.suffix

    # Save the transformed data to a new CSV file
    transformed_df.to_csv(output_file, index=False)

    print(f'Transformed dataset saved to {output_file}')


MAX_JSON_PARSE_ATTEMPTS = 4
def generate_messages(client, num_messages: int, dest: str, batch_size: int, use_instructor: bool = False):

    """
    Generates message pairs and writes them to a CSV file.

    Args:
        client: The OpenAI client configured for either local or OpenAI API interaction.
        num_messages (int): The total number of message pairs to generate.
        dest (str): The destination file path for the output CSV.
        batch_size (int): The number of message pairs to generate in each batch.

    """
    total_generated = 0
    while total_generated < num_messages:
        category = random.choice(CATEGORIES)
        prompt = f'Generate {batch_size} pairs of short instant messages where each pair has a non-disrespectful (respectful or neutral) message and then a corresponding disrespectful message exemplifying "{category}", formatted in JSON with property names "nondisrespectful" and "disrespecful" accordingly.'

        if use_instructor:
            # When use_instructor is True, use the instructor response_model to preprocess the messages
            response = client.chat.completions.create(
                model=LOCAL_MODEL if USING_LOCAL else OPENAI_MODEL,
                temperature=0.3,
                messages=[{"role": "user", "content": prompt}],
                response_model=CategorisedMessages  # This assumes the response_model can process the response into CategorisedMessages
            )
            message_pairs = response.messages
        else:
            # we can't guarantee that a completion will fit the required schema so if it doesn't match we just throw it away and try again. 
            attempts = 0
            successfully_parsed_response = False
            while not successfully_parsed_response and attempts < MAX_JSON_PARSE_ATTEMPTS:
                try:

                    # When use_instructor is False, extract message_pairs directly from the raw OpenAI response
                    response = client.chat.completions.create(
                        model=LOCAL_MODEL if USING_LOCAL else OPENAI_MODEL,
                        temperature=0.3,
                        messages=[{"role": "user", "content": prompt}]
                    )
                    #print(response.json())
                    content = strip_code_block(response.choices[0].message.content)
                    # Splitting the content by double line breaks to separate each message pair
                    #print("=== content: ")
                    #print(content)
                    pairs = json.loads(content)
                    #print(json.dumps(pairs, indent=2))

                    message_pairs = []
                    for pair in pairs:
                        # Extract respectful and disrespectful messages directly from the dictionary
                        nondisrespectful = pair["nondisrespectful"]
                        disrespectful = pair["disrespectful"]
                        message_pairs.append(MessagePair(nondisrespectful=nondisrespectful, disrespectful=disrespectful))
                    successfully_parsed_response = True  # JSON was successfully decoded, exit loop
                except JSONDecodeError:
                    print(f"Attempt {attempts + 1} failed with JSONDecodeError which means the generated response wasn't valid JSON. Trying again...")
                    attempts += 1
                except Exception as e:
                    # Catch other exceptions to prevent an unexpected error from breaking the loop
                    print(f"An unexpected error occurred: {e}")
                    break

            if not successfully_parsed_response:
                print("Failed to process JSON after 10 attempts.")
        write_to_csv(dest, message_pairs, category)
        total_generated += len(message_pairs)
def write_to_csv(dest, message_pairs, category):
    """
    Writes the generated message pairs to a CSV file.

    Args:
        dest (str): The destination file path for the output CSV.
        message_pairs (List[MessagePair]): The list of message pairs to be written.
        category (str): The category of disrespectfulness for the message pairs.

    Returns:
        None
    """
    # Check if the destination file exists before opening it.
    file_exists = os.path.isfile(dest)
    mode = 'a' if file_exists else 'w'
    
    with open(dest, mode, newline='', encoding='utf-8') as csvfile:
        fieldnames = ['class', 'nondisrespectful', 'disrespectful']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        
        # Write the header only if the file did not exist previously.
        if not file_exists:
            writer.writeheader()
        
        for pair in message_pairs:
            writer.writerow({'class': category, 'nondisrespectful': pair.nondisrespectful, 'disrespectful': pair.disrespectful})

if __name__ == "__main__":
    # Command-line interface setup for the script.
    parser = argparse.ArgumentParser(description="Generate chat messages with specified negative qualities.")
    parser.add_argument("--num", type=int, default=20, help="Number of message pairs to generate")
    parser.add_argument("--openai", action='store_true', help="Use OpenAI instead of local model")
    parser.add_argument("--dest", type=str, default="messages.csv", help="Destination CSV file for the messages")
    parser.add_argument("--use-instructor", action='store_true', help="use the instructor library for type checking and response validation (default: False)")
    parser.add_argument("--batch-size", type=int, default=50, help="Number of message pairs per batch (default: 50)")

    args = parser.parse_args()

    # Determine whether to use the local API or OpenAI based on command-line arguments.
    USING_LOCAL = not args.openai
    use_instructor = args.use_instructor

    # Configure the OpenAI client for either local or OpenAI API use.
    client = instructor.patch(OpenAI(
        base_url=LOCAL_API_URL,
        api_key=LOCAL_API_KEY,
    ), mode=instructor.Mode.JSON) if USING_LOCAL else instructor.patch(OpenAI(), mode=instructor.Mode.JSON)

    # Generate the messages and write them to the specified CSV file.
    print(f"Generating {args.num} messages...")
    generate_messages(client, args.num, args.dest, args.batch_size, use_instructor)
    print(f"Messages written to {args.dest}")

    print(f"Now deduplicating messages...")
    dedupe_messages(args.dest)
    print(f"Deduplicating messages done.")

    print(f"Now creating the binary classifer dataset…")
    create_binary_classification_dataset(args.dest)
    print(f"Binary classifier dataset created.")
