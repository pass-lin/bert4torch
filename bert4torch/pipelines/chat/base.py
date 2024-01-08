import os
import torch
from typing import Union, Optional
from bert4torch.models import build_transformer_model
from bert4torch.snippets import log_warn_once, cuda_empty_cache, is_streamlit_available


if is_streamlit_available():
    import streamlit as st


class Chat:
    '''聊天类
    :param model_path: str, 模型权重地址，可以是所在文件夹、文件地址、文件地址列表
    :param half: bool, 是否半精度
    :param quantization_config: dict, 模型量化使用到的参数, eg. {'quantization_method':'cpm_kernels', 'quantization_bit':8}
    :param generation_config: dict, genrerate使用到的参数, eg. {'mode':'random_sample', 'max_length':2048, 'default_rtype':'logits', 'use_states':True}
    '''
    def __init__(self, model_path, half=True, quantization_config=None, generation_config=None, **kwargs):
        self.device = 'cuda' if torch.cuda.is_available() else 'cpu'
        self.model_path = model_path
        self.checkpoint_path = model_path
        self.config_path = os.path.join(model_path, 'bert4torch_config.json')
        self.generation_config = generation_config if generation_config is not None else kwargs
        self.half = half
        self.quantization_config = quantization_config
        self.tokenizer = self.build_tokenizer()
        self.generation_config['tokenizer'] = self.tokenizer
        self.model = self.build_model()

    def no_history_states(self) -> bool:
        '''不使用history的states'''
        return self.generation_config.get('states') is None
    
    def build_prompt(self, query, history) -> str:
        '''对query和history进行处理，生成进入模型的text
        :param query: str, 最近的一次user的input
        :param history: List, 历史对话记录，格式为[(input1, response1), (input2, response2)]
        '''
        raise NotImplementedError
    
    def build_tokenizer(self):
        from transformers import AutoTokenizer
        return AutoTokenizer.from_pretrained(self.model_path, trust_remote_code=True)

    def build_model(self):
        model = build_transformer_model(config_path=self.config_path, checkpoint_path=self.checkpoint_path)
        # 半精度
        if self.half:
            model = model.half()
        # 量化
        if self.quantization_config is not None:
            model = model.quantize(**self.quantization_config)
        return model.to(self.device)
    
    def process_response(self, response:Union[str,tuple,list], history:Optional[list]=None):
        '''对response进行后处理，可自行继承后来自定义'''
        if isinstance(response, str):
            return response
        elif isinstance(response, (tuple, list)):  # response, states
            assert len(response) == 2
            self.generation_config['states'] = response[1]
            return response[0]
        return response

    def chat(self, query:Union[str,list], history=[]):
        if isinstance(query, str):
            prompt = self.build_prompt(query, history)
        elif isinstance(query, list):
            prompt = [self.build_prompt(q, history) for q in query]
        response = self.model.generate(prompt, **self.generation_config)
        return self.process_response(response, history=history)

    def stream_chat(self, query:str, history=[]):
        '''单条样本stream输出预测的结果'''
        prompt = self.build_prompt(query, history)
        for response in self.model.stream_generate(prompt, **self.generation_config):
            yield self.process_response(response, history)


class ChatCli(Chat):
    '''在命令行中交互的demo'''
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.init_str = "输入内容进行对话，clear清空对话历史，stop终止程序"
        self.history_maxlen = 3

    def build_cli_text(self, history):
        '''构建命令行终端显示的text'''
        prompt = self.init_str
        for query, response in history:
            prompt += f"\n\nUser：{query}"
            prompt += f"\n\nAssistant：{response}"
        return prompt

    def run(self, stream=True):
        import platform
        os_name = platform.system()
        previous_history, history = [], []
        clear_command = 'cls' if os_name == 'Windows' else 'clear'
        print(self.init_str)
        while True:
            query = input("\nUser: ")
            if query.strip() == "stop":
                break
            if query.strip() == "clear":
                previous_history, history = [], []
                if 'states' in self.generation_config:
                    self.generation_config.pop('states')
                os.system(clear_command)
                print(self.init_str)
                continue
            
            prompt = self.build_prompt(query, history)
            if stream:
                for response in self.model.stream_generate(prompt, **self.generation_config):
                    response = self.process_response(response, history)
                    new_history = history + [(query, response)]
                    os.system(clear_command)
                    print(self.build_cli_text(previous_history + new_history), flush=True)
            else:
                response = self.model.generate(prompt, **self.generation_config)
                response = self.process_response(response, history)
                new_history = history + [(query, response)]
            
            os.system(clear_command)
            print(self.build_cli_text(previous_history + new_history), flush=True)
            history = new_history[-self.history_maxlen:]
            if len(new_history) > self.history_maxlen:
                previous_history += new_history[:-self.history_maxlen]
            cuda_empty_cache()


def extend_with_cli(InputModel):
    """添加ChatCliDemo"""
    class ChatDemo(InputModel, ChatCli):
        pass
    return ChatDemo


class ChatWebGradio(Chat):
    '''gradio实现的网页交互的demo
    默认是stream输出，默认history不会删除，需手动清理
    '''
    def __init__(self, *args, max_length=4096, **kwargs):
        super().__init__(*args, **kwargs)
        import gradio as gr
        self.gr = gr
        self.max_length = max_length
        self.max_repetition_penalty = 10
        self.stream = True  # 一般都是流式，因此未放在页面配置项
        log_warn_once('`gradio` changes frequently, the code is successfully tested under 3.44.4')

    def reset_user_input(self):
        return self.gr.update(value='')

    @staticmethod
    def reset_state():
        return [], []

    def set_generation_config(self, max_length, top_p, temperature, repetition_penalty):
        '''根据web界面的参数修改生成参数'''
        self.generation_config['max_length'] = max_length
        self.generation_config['top_p'] = top_p
        self.generation_config['temperature'] = temperature
        self.generation_config['repetition_penalty'] = repetition_penalty

    def __stream_predict(self, input, chatbot, history, max_length, top_p, temperature, repetition_penalty):
        '''流式生成'''
        self.set_generation_config(max_length, top_p, temperature, repetition_penalty)
        chatbot.append((input, ""))
        input_text = self.build_prompt(input, history)
        for response in self.model.stream_generate(input_text, **self.generation_config):
            response = self.process_response(response, history)
            chatbot[-1] = (input, response)
            new_history = history + [(input, response)]
            yield chatbot, new_history
        cuda_empty_cache()  # 清理显存

    def __predict(self, input, chatbot, history, max_length, top_p, temperature, repetition_penalty):
        '''一次性生成'''
        self.set_generation_config(max_length, top_p, temperature, repetition_penalty)
        chatbot.append((input, ""))
        input_text = self.build_prompt(input, history)
        response = self.model.generate(input_text, **self.generation_config)
        response = self.process_response(response, history)
        chatbot[-1] = (input, response)
        new_history = history + [(input, response)]
        cuda_empty_cache()  # 清理显存
        return chatbot, new_history

    def run(self, **launch_configs):
        with self.gr.Blocks() as demo:
            self.gr.HTML("""<h1 align="center">Chabot Web Demo</h1>""")

            chatbot = self.gr.Chatbot()
            with self.gr.Row():
                with self.gr.Column(scale=4):
                    with self.gr.Column(scale=12):
                        user_input = self.gr.Textbox(show_label=False, placeholder="Input...", lines=10) # .style(container=False)
                    with self.gr.Column(min_width=32, scale=1):
                        submitBtn = self.gr.Button("Submit", variant="primary")
                with self.gr.Column(scale=1):
                    emptyBtn = self.gr.Button("Clear History")
                    max_length = self.gr.Slider(0, self.max_length, value=self.max_length//2, step=1.0, label="Maximum length", interactive=True)
                    top_p = self.gr.Slider(0, 1, value=0.7, step=0.01, label="Top P", interactive=True)
                    temperature = self.gr.Slider(0, 1, value=0.95, step=0.01, label="Temperature", interactive=True)
                    repetition_penalty = self.gr.Slider(0, self.max_repetition_penalty, value=1, step=0.1, label="Repetition penalty", interactive=True)

            history = self.gr.State([])
            if self.stream:
                submitBtn.click(self.__stream_predict, [user_input, chatbot, history, max_length, top_p, temperature, repetition_penalty], [chatbot, history], show_progress=True)
            else:
                submitBtn.click(self.__predict, [user_input, chatbot, history, max_length, top_p, temperature, repetition_penalty], [chatbot, history], show_progress=True)

            submitBtn.click(self.reset_user_input, [], [user_input])
            emptyBtn.click(self.reset_state, outputs=[chatbot, history], show_progress=True)

        demo.queue().launch(**launch_configs)


def extend_with_web_gradio(InputModel):
    """添加ChatWebDemo"""
    class ChatDemo(InputModel, ChatWebGradio):
        pass
    return ChatDemo


class ChatWebStreamlit(Chat):
    def __init__(self, *args, max_length=4096, **kwargs):
        st.set_page_config(
            page_title="Chabot Web Demo",
            page_icon=":robot:",
            layout="wide"
        )
        super().__init__(*args, **kwargs)
        self.max_length = max_length

    @st.cache_resource
    def build_model(_self):
        return super().build_model()
    
    @st.cache_resource
    def build_tokenizer(_self):
        return super().build_tokenizer()
    
    def run(self):
        if "history" not in st.session_state:
            st.session_state.history = []
        if "past_key_values" not in st.session_state:
            st.session_state.past_key_values = None

        max_length = st.sidebar.slider("max_length", 0, self.max_length, self.max_length//2, step=1)
        top_p = st.sidebar.slider("top_p", 0.0, 1.0, 0.8, step=0.01)
        temperature = st.sidebar.slider("temperature", 0.0, 1.0, 0.6, step=0.01)

        buttonClean = st.sidebar.button("清理会话历史", key="clean")
        if buttonClean:
            st.session_state.history = []
            st.session_state.past_key_values = None
            cuda_empty_cache()
            st.rerun()

        for i, message in enumerate(st.session_state.history):
            with st.chat_message(name="user", avatar="user"):
                st.markdown(message[0])

            with st.chat_message(name="assistant", avatar="assistant"):
                st.markdown(message[1])

        with st.chat_message(name="user", avatar="user"):
            input_placeholder = st.empty()
        with st.chat_message(name="assistant", avatar="assistant"):
            message_placeholder = st.empty()

        prompt_text = st.chat_input("请输入您的问题")
        if prompt_text:
            input_placeholder.markdown(prompt_text)
            history = st.session_state.history
            past_key_values = st.session_state.past_key_values
            self.generation_config['max_length'] = max_length
            self.generation_config['top_p'] = top_p
            self.generation_config['temperature'] = temperature

            input_text = self.build_prompt(prompt_text, history)
            for response in self.model.stream_generate(input_text, **self.generation_config):
                message_placeholder.markdown(response)
            st.session_state.history = history + [(prompt_text, response)]
            st.session_state.past_key_values = past_key_values


def extend_with_web_streamlit(InputModel):
    """添加ChatWebDemo"""
    class ChatDemo(InputModel, ChatWebStreamlit):
        pass
    return ChatDemo