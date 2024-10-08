import datetime
import os
import time
import json
import boto3
import io

import litellm
from wandb.sdk.data_types.trace_tree import Trace
from transformers import AutoTokenizer

class MessageTokenIterator:
    def __init__(self, stream):
        self.byte_iterator = iter(stream)
        self.buffer = io.BytesIO()
        self.read_pos = 0

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            self.buffer.seek(self.read_pos)
            line = self.buffer.readline()

            # print(line)
            if line and line[-1] == ord("\n"):
                self.read_pos += len(line)
                full_line = line[:-1].decode("utf-8")
                # print(full_line)
                line_data = json.loads(full_line.lstrip("data:").rstrip("/n"))
                return line_data["choices"][0]["delta"].get("content", "")
            chunk = next(self.byte_iterator)
            self.buffer.seek(0, io.SEEK_END)
            self.buffer.write(chunk["PayloadPart"]["Bytes"])

def get_realtime_response_stream(sagemaker_runtime, endpoint_name, payload):
    response_stream = sagemaker_runtime.invoke_endpoint_with_response_stream(
        EndpointName=endpoint_name,
        Body=json.dumps(payload),
        ContentType="application/json",
        CustomAttributes='accept_eula=false'
    )
    return response_stream

class AICaller:
    def __init__(self, model: str, api_base: str = "", hf_model_name: str = ""):
        """
        Initializes an instance of the AICaller class.

        Parameters:
            model (str): The name of the model to be used.
            api_base (str): The base API url to use in case model is set to Ollama or Hugging Face
        """
        self.model = model
        self.api_base = api_base
        self.hf_model_name = hf_model_name
        if "sagemaker" in self.model:
            self.tokenizer = AutoTokenizer.from_pretrained(hf_model_name)

    def call_model(self, prompt: dict, max_tokens=4096):
        """
        Call the language model with the provided prompt and retrieve the response.

        Parameters:
            prompt (dict): The prompt to be sent to the language model.
            max_tokens (int, optional): The maximum number of tokens to generate in the response. Defaults to 4096.

        Returns:
            tuple: A tuple containing the response generated by the language model, the number of tokens used from the prompt, and the total number of tokens in the response.
        """
        if "system" not in prompt or "user" not in prompt:
            raise KeyError(
                "The prompt dictionary must contain 'system' and 'user' keys."
            )
        if prompt["system"] == "":
            messages = [{"role": "user", "content": prompt["user"]}]
        else:
            messages = [
                {"role": "system", "content": prompt["system"]},
                {"role": "user", "content": prompt["user"]},
            ]

        print("*"*20, "Cover-agent input", "*"*20)
        print(messages[-1]["content"])
        completion_params = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "hf_model_name": self.hf_model_name,
            "stream": True,
            "temperature": 0.2,
        }
        if "sagemaker" in self.model:
            
            smr_client = boto3.client("sagemaker-runtime", region_name = os.environ["AWS_REGION_NAME"],aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"], aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"])

            response_stream = get_realtime_response_stream(smr_client,self.model.split("/")[-1], completion_params)
            output = []
            for token in MessageTokenIterator(response_stream["Body"]):
                # pass
                output.append(token)
                print(token or "", end="", flush=True)
            output = "".join(output)
            model_response = {
                "choices": [{"message": {"content": output}}],
                "usage": {
                    "prompt_tokens": len(self.tokenizer.tokenize(self.tokenizer.apply_chat_template(messages, add_generation_prompt=True, tokenize=False))),
                    "completion_tokens": len(self.tokenizer.tokenize(output))
                }
            } 
        else:

            # Default Completion:q
            # parameters
            completion_params.pop("hf_model_name")
            

            # API base exception for OpenAI Compatible, Ollama and Hugging Face models
            if (
                "ollama" in self.model
                or "huggingface" in self.model
                or self.model.startswith("openai/")
            ):
                completion_params["api_base"] = self.api_base

            response = litellm.completion(**completion_params)

            chunks = []
            print("Streaming results from LLM model...")
            try:
                for chunk in response:
                    print(chunk.choices[0].delta.content or "", end="", flush=True)
                    chunks.append(chunk)
                    time.sleep(
                        0.01
                    )  # Optional: Delay to simulate more 'natural' response pacing
            except Exception as e:
                print(f"Error during streaming: {e}")
            print("\n")

            model_response = litellm.stream_chunk_builder(chunks, messages=messages)

        if "WANDB_API_KEY" in os.environ:
            root_span = Trace(
                name="inference_"
                + datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S"),
                kind="llm",  # kind can be "llm", "chain", "agent" or "tool
                inputs={
                    "user_prompt": prompt["user"],
                    "system_prompt": prompt["system"],
                },
                outputs={
                    "model_response": model_response["choices"][0]["message"]["content"]
                },
            )
            root_span.log(name="inference")

        # Returns: Response, Prompt token count, and Response token count
        return (
            model_response["choices"][0]["message"]["content"],
            int(model_response["usage"]["prompt_tokens"]),
            int(model_response["usage"]["completion_tokens"]),
        )
