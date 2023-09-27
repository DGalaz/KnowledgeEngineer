from __future__ import annotations

import json
import os
import time
import traceback
from json import JSONDecodeError

import openai
from dotenv import load_dotenv
from twisted.internet.defer import inlineCallbacks
from twisted.logger import Logger

import KbServerApp.colors

from KbServerApp.OpenAI_API_Costs import OpenAI_API_Costs
from KbServerApp.db import DB
from KbServerApp.defered import as_deferred

load_dotenv()


class AI:
    log = Logger(namespace='AI')
    memory = DB('Memory')

    openai.api_key = os.getenv('OPENAI_API_KEY')
    models = openai.Model.list()

    @classmethod
    def list_models(cls):
        l: list[str] = []
        for m in cls.models.data:
            l.append(m['id'])
        return l

    def __init__(self,
                 model: str = "gpt-4",
                 temperature: float = 0,
                 max_tokens: int = 2000,
                 mode: str = 'complete',
                 messages: [dict[str, str]] = None,
                 answer: str = None,
                 files: dict[str, str] = None,
                 e_stats: dict[str, float] = None
                 ):
        self.temperature: float = temperature
        self.max_tokens: int = max_tokens
        self.model: str = model
        self.mode: str = mode
        self.messages: [dict[str, str]] = messages
        if messages is None:
            self.messages = []
        self.answer: str = answer,
        if answer is None:
            self.messages = []

        self.files: dict[str, str] = files
        if files is None:
            self.files = {}

        self.e_stats: dict[str, float] = e_stats
        if e_stats is None:
            self.e_stats = {
                'prompt_tokens': 0.0,
                'completion_tokens': 0.0,
                'total_tokens': 0.0,
                'sp_cost': 0.0,
                'sc_cost': 0.0,
                's_total': 0.0,
                'elapsed_time': 0.0,
            }

        try:
            openai.Model.retrieve(model)
        except openai.InvalidRequestError as e:
            AI.log.error(f"Error: {e}")
            AI.log.warn(
                f"Model {model} not available for provided API key. Reverting "
                "to text-davinci-003. Sign up for the GPT-4 wait list here: "
                "https://openai.com/waitlist/gpt-4-api"
            )
            self.model = "gpt-3.5-turbo"

        # GptLogger.log('SYSTEM', f"Using model {self.model} in mode {self.mode}")

    def read_file(self, name: str) -> dict[str, str]:

        try:
            file_msgs = self.memory[name]
        except Exception as err:
            self.log.error("Error while reading file for AI...")
            raise
        file_msg = file_msgs[0]
        file_contents = file_msg['content']
        return {'role': 'function', 'name': 'read_file', 'content': file_contents}

    def write_file(self, name: str, contents: str) -> dict[str, str]:
        try:
            self.memory[name] = contents
        except Exception as err:
            self.log.error("Error while writing file for AI...")
            raise
        self.log.info("Writing<<{name}", name=name)
        return {'role': 'function', 'name': 'write_file', 'content': 'Done.'}

    # def check_python(self, name: str, contents: str) -> str:
    #     try:
    #         self.memory[name] = contents
    #     except Exception as err:
    #         self.log.error("Error while writing file for AI...")
    #         raise
    #     self.log.info("Writing<<{name}", name=name)
    #     return {'role': 'function', 'name': 'write_file', 'content': 'Done.'}
    #
    functions = [
        {
            "name": "read_file",
            "description": "Read the contents of a named file",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The name of the file to read",
                    },
                },
                "required": ["name"],
            },
        },
        {
            "name": "write_file",
            "description": "Write the contents to a named file",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The name of the file to write",
                    },
                    "contents": {
                        "type": "string",
                        "description": "The contents of the file",
                    },
                },
                "required": ["name", "contents"],
            },
        }
    ]
    available_functions = {
        "read_file": read_file,
        "write_file": write_file,
    }  # only one function in this example, but you can have multiple

    @inlineCallbacks
    def generate(self, step, user_messages: list[dict[str, str]]) -> dict[str, str]:

        self.answer = f'Log of Step: {step.name} : {step.prompt_name}\n'
        pricing = OpenAI_API_Costs[self.model]

        while user_messages:
            msg = user_messages.pop(0)
            if msg['role'] != 'exec':
                self.messages.append(msg)
                self.log.info("    --> msg:{msg}", msg=msg)
                continue

            repeat = True
            while repeat:
                repeat = False

                step.update_gui()
                ai_response = yield self.chat(self.messages)
                response_message = {'role': ai_response.choices[0].message['role'],
                                    'content': ai_response.choices[0].message['content']
                                    }
                function_name = None
                function_args = None
                if ai_response.choices[0].finish_reason == 'function_call':
                    # if ai_response.choices[0].message.get("function_call"):
                    call_function = ai_response.choices[0].message.get("function_call")
                    function_name = call_function["name"]
                    try:
                        t_args = call_function["arguments"]
                        begin = t_args.find('"contents": "')
                        if begin == -1:
                            t1_args = t_args
                        else:
                            end = t_args.rfind('"')
                            t1_args = t_args[:begin] + t_args[begin:end].replace('\n', '\\n') + t_args[end:]
                        function_args = json.loads(t1_args)
                    except JSONDecodeError as err:
                        err_msg = err.args[0]
                        self.log.warn("While parsing arguments for function call {name}\n{err_msg}",
                                       name=function_name, err_msg=err_msg)
                        # Okay Send error back to AI...
                        repeat = True
                        response_message['function_call'] = {'name': function_name, 'arguments': call_function["arguments"]}
                        response_message['content'] = f'Arguments are not valid Jason\n{err_msg}'
                        self.messages.append(response_message)
                        self.log.info("    --> msg:{msg}", msg=response_message)
                        continue

                    response_message['function_call'] = {'name': function_name, 'arguments': call_function["arguments"]}
                else:
                    self.answer = f"{self.answer}\n\n - {response_message['content']}"

                self.messages.append(response_message)
                self.log.info("    <-- msg:{msg}", msg=response_message)  # Display with last message
                if ai_response.choices[0].finish_reason == 'function_call':
                    new_msg = self.available_functions[function_name](self, **function_args)
                    self.messages.append(new_msg)
                    self.log.info("    --> msg:{msg}", msg=new_msg)
                    repeat = True

                # Gather Answer
                self.e_stats['prompt_tokens'] = \
                    self.e_stats['prompt_tokens'] + ai_response['usage']['prompt_tokens']
                self.e_stats['completion_tokens'] = \
                    self.e_stats['completion_tokens'] + ai_response['usage']['completion_tokens']

        self.e_stats['sp_cost'] = pricing['input'] * (self.e_stats['prompt_tokens'] / 1000.0)
        self.e_stats['sc_cost'] = pricing['output'] * (self.e_stats['completion_tokens'] / 1000.0)
        self.e_stats['s_total'] = self.e_stats['sp_cost'] + self.e_stats['sc_cost']

        return self.answer

    @inlineCallbacks
    def chat(self, messages: list[dict[str, str]]) -> dict:

        # AI.log.info(f"Calling {self.model} chat with messages: ")
        try:
            response = yield as_deferred(openai.ChatCompletion.acreate(
                messages=messages,
                model=self.model,
                temperature=self.temperature,
                functions=self.functions,
                function_call="auto",
            ))
        except Exception as err:
            self.log.error("Call to ChatGpt returned error: {err}", err=err)
            raise

        # AI.log.info(f"{self.model} chat Response")
        return response

    @inlineCallbacks
    def complete(self, prompt: str) -> dict:

        # AI.log.info(f"Calling {self.model} complete with prompt: ")
        completion = yield as_deferred(openai.Completion.acreate(
            model=self.model,
            prompt=prompt,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        ))
        # AI.log.info(f"{self.model} Response")
        return completion

    def to_json(self) -> dict:
        return {
            'model': self.model,
            'temperature': self.temperature,
            'max_tokens': self.max_tokens,
            'mode': self.mode,
            'messages': self.messages,
            'answer': self.answer,
            'files': self.files,
            'e_stats': self.e_stats
        }

    @classmethod
    def from_json(cls, param) -> AI:
        return cls(**param)

if __name__ == "__main__":
    my_models = {m.id for m in AI.models['data'] }
    print(f"Models: {AI.models['data']}")
    for m in sorted(my_models):
        print(f'\t{m}')