import ast
import functools
import glob
import inspect
import queue
import shutil
import sys
import os
import time
import traceback
import types
import typing
import warnings
from datetime import datetime
import filelock
import psutil

os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['BITSANDBYTES_NOWELCOME'] = '1'
warnings.filterwarnings('ignore', category=UserWarning, message='TypedStorage is deprecated')

from enums import DocumentChoices, LangChainMode
from loaders import get_loaders
from utils import set_seed, clear_torch_cache, save_generate_output, NullContext, wrapped_partial, EThread, get_githash, \
    import_matplotlib, get_device, makedirs, get_kwargs, start_faulthandler

start_faulthandler()
import_matplotlib()

SEED = 1236
set_seed(SEED)

from typing import Union

import fire
import torch
from transformers import GenerationConfig, AutoModel, TextIteratorStreamer
from accelerate import init_empty_weights, infer_auto_device_map

from prompter import Prompter, inv_prompt_type_to_model_lower, non_hf_types, PromptType, get_prompt
from stopping import get_stopping

eval_extra_columns = ['prompt', 'response', 'score']

langchain_modes = [x.value for x in list(LangChainMode)]

scratch_base_dir = '/tmp/'


def main(
        load_8bit: bool = False,
        load_4bit: bool = False,
        load_half: bool = True,
        infer_devices: bool = True,
        base_model: str = '',
        tokenizer_base_model: str = '',
        lora_weights: str = "",
        gpu_id: int = 0,
        compile_model: bool = True,

        prompt_type: Union[int, str] = None,
        prompt_dict: typing.Dict = None,
        # input to generation
        temperature: float = None,
        top_p: float = None,
        top_k: int = None,
        num_beams: int = None,
        repetition_penalty: float = None,
        num_return_sequences: int = None,
        do_sample: bool = None,
        max_new_tokens: int = None,
        min_new_tokens: int = None,
        early_stopping: Union[bool, str] = None,
        max_time: float = None,

        memory_restriction_level: int = None,
        debug: bool = False,
        save_dir: str = None,
        share: bool = True,
        local_files_only: bool = False,
        resume_download: bool = True,
        use_auth_token: Union[str, bool] = False,
        trust_remote_code: Union[str, bool] = True,
        offload_folder: str = "offline_folder",

        src_lang: str = "English",
        tgt_lang: str = "Russian",

        cli: bool = False,
        cli_loop: bool = True,
        gradio: bool = True,
        gradio_offline_level: int = 0,
        chat: bool = True,
        chat_context: bool = False,
        stream_output: bool = True,
        show_examples: bool = None,
        verbose: bool = False,
        h2ocolors: bool = False,
        height: int = 600,
        show_lora: bool = True,
        login_mode_if_model0: bool = False,
        block_gradio_exit: bool = True,
        concurrency_count: int = 1,
        api_open: bool = False,
        allow_api: bool = True,
        input_lines: int = 1,
        auth: typing.List[typing.Tuple[str, str]] = None,
        max_max_time=None,
        max_max_new_tokens=None,

        sanitize_user_prompt: bool = True,
        sanitize_bot_response: bool = False,

        extra_model_options: typing.List[str] = [],
        extra_lora_options: typing.List[str] = [],

        score_model: str = 'OpenAssistant/reward-model-deberta-v3-large-v2',
        auto_score: bool = True,

        eval_filename: str = None,
        eval_prompts_only_num: int = 0,
        eval_prompts_only_seed: int = 1234,
        eval_as_output: bool = False,

        langchain_mode: str = 'Disabled',
        visible_langchain_modes: list = ['UserData', 'MyData'],
        document_choice: list = [DocumentChoices.All_Relevant.name],
        user_path: str = None,
        detect_user_path_changes_every_query: bool = False,
        load_db_if_exists: bool = True,
        keep_sources_in_context: bool = False,
        db_type: str = 'chroma',
        use_openai_embedding: bool = False,
        use_openai_model: bool = False,
        hf_embedding_model: str = None,
        allow_upload_to_user_data: bool = True,
        allow_upload_to_my_data: bool = True,
        enable_url_upload: bool = True,
        enable_text_upload: bool = True,
        enable_sources_list: bool = True,
        chunk: bool = True,
        chunk_size: int = 512,
        top_k_docs: int = 3,  # FIXME: Can go back to 4 once https://github.com/h2oai/h2ogpt/issues/192 fixed
        n_jobs: int = -1,
        enable_captions: bool = True,
        captions_model: str = "Salesforce/blip-image-captioning-base",
        pre_load_caption_model: bool = False,
        caption_gpu: bool = True,
        enable_ocr: bool = False,
):
    """

    :param load_8bit: load model in 8-bit using bitsandbytes
    :param load_4bit: load model in 4-bit using bitsandbytes
    :param load_half: load model in float16
    :param infer_devices: whether to control devices with gpu_id.  If False, then spread across GPUs
    :param base_model: model HF-type name.  If use --base_model to preload model, cannot unload in gradio in models tab
    :param tokenizer_base_model: tokenizer HF-type name.  Usually not required, inferred from base_model.
    :param lora_weights: LORA weights path/HF link
    :param gpu_id: if infer_devices, then use gpu_id for cuda device ID, or auto mode if gpu_id != -1
    :param compile_model Whether to compile the model
    :param prompt_type: type of prompt, usually matched to fine-tuned model or plain for foundational model
    :param prompt_dict: If prompt_type=custom, then expects (some) items returned by get_prompt(..., return_dict=True)
    :param temperature: generation temperature
    :param top_p: generation top_p
    :param top_k: generation top_k
    :param num_beams: generation number of beams
    :param repetition_penalty: generation repetition penalty
    :param num_return_sequences: generation number of sequences (1 forced for chat)
    :param do_sample: generation sample
    :param max_new_tokens: generation max new tokens
    :param min_new_tokens: generation min tokens
    :param early_stopping: generation early stopping
    :param max_time: maximum time to allow for generation
    :param memory_restriction_level: 0 = no restriction to tokens or model, 1 = some restrictions on token 2 = HF like restriction 3 = very low memory case
    :param debug: enable debug mode
    :param save_dir: directory chat data is saved to
    :param share: whether to share the gradio app with sharable URL
    :param local_files_only: whether to only use local files instead of doing to HF for models
    :param resume_download: whether to resume downloads from HF for models
    :param use_auth_token: whether to use HF auth token (requires CLI did huggingface-cli login before)
    :param trust_remote_code: whether to use trust any code needed for HF model
    :param offload_folder: path for spilling model onto disk
    :param src_lang: source languages to include if doing translation (None = all)
    :param tgt_lang: target languages to include if doing translation (None = all)
    :param cli: whether to use CLI (non-gradio) interface.
    :param cli_loop: whether to loop for CLI (False usually only for testing)
    :param gradio: whether to enable gradio, or to enable benchmark mode
    :param gradio_offline_level: > 0, then change fonts so full offline
           == 1 means backend won't need internet for fonts, but front-end UI might if font not cached
           == 2 means backend and frontend don't need internet to download any fonts.
           Note: Some things always disabled include HF telemetry, gradio telemetry, chromadb posthog that involve uploading.
           This option further disables google fonts for downloading, which is less intrusive than uploading,
           but still required in air-gapped case.  The fonts don't look as nice as google fonts, but ensure full offline behavior.
    :param chat: whether to enable chat mode with chat history
    :param chat_context: whether to use extra helpful context if human_bot
    :param stream_output: whether to stream output from generate
    :param show_examples: whether to show clickable examples in gradio
    :param verbose: whether to show verbose prints
    :param h2ocolors: whether to use H2O.ai theme
    :param height: height of chat window
    :param show_lora: whether to show LORA options in UI (expert so can be hard to understand)
    :param login_mode_if_model0: set to True to load --base_model after client logs in, to be able to free GPU memory when model is swapped
    :param block_gradio_exit: whether to block gradio exit (used for testing)
    :param concurrency_count: gradio concurrency count (1 is optimal for LLMs)
    :param api_open: If False, don't let API calls skip gradio queue
    :param allow_api: whether to allow API calls at all to gradio server
    :param input_lines: how many input lines to show for chat box (>1 forces shift-enter for submit, else enter is submit)
    :param auth: gradio auth for launcher in form [(user1, pass1), (user2, pass2), ...]
                 e.g. --auth=[('jon','password')] with no spaces
    :param max_max_time: Maximum max_time for gradio slider
    :param max_max_new_tokens: Maximum max_new_tokens for gradio slider
    :param sanitize_user_prompt: whether to remove profanity from user input
    :param sanitize_bot_response: whether to remove profanity and repeat lines from bot output (about 2x slower generation for long streaming cases due to better_profanity being slow)
    :param extra_model_options: extra models to show in list in gradio
    :param extra_lora_options: extra LORA to show in list in gradio
    :param score_model: which model to score responses (None means no scoring)
    :param auto_score: whether to automatically score responses
    :param eval_filename: json file to use for evaluation, if None is sharegpt
    :param eval_prompts_only_num: for no gradio benchmark, if using eval_filename prompts for eval instead of examples
    :param eval_prompts_only_seed: for no gradio benchmark, seed for eval_filename sampling
    :param eval_as_output: for no gradio benchmark, whether to test eval_filename output itself
    :param langchain_mode: Data source to include.  Choose "UserData" to only consume files from make_db.py.
           WARNING: wiki_full requires extra data processing via read_wiki_full.py and requires really good workstation to generate db, unless already present.
    :param user_path: user path to glob from to generate db for vector search, for 'UserData' langchain mode.
           If already have db, any new/changed files are added automatically if path set, does not have to be same path used for prior db sources
    :param detect_user_path_changes_every_query: whether to detect if any files changed or added every similarity search (by file hashes).
           Expensive for large number of files, so not done by default.  By default only detect changes during db loading.
    :param visible_langchain_modes: dbs to generate at launch to be ready for LLM
           Can be up to ['wiki', 'wiki_full', 'UserData', 'MyData', 'github h2oGPT', 'DriverlessAI docs']
           But wiki_full is expensive and requires preparation
           To allow scratch space only live in session, add 'MyData' to list
           Default: If only want to consume local files, e.g. prepared by make_db.py, only include ['UserData']
           FIXME: Avoid 'All' for now, not implemented
    :param document_choice: Default document choice when taking subset of collection
    :param load_db_if_exists: Whether to load chroma db if exists or re-generate db
    :param keep_sources_in_context: Whether to keep url sources in context, not helpful usually
    :param db_type: 'faiss' for in-memory or 'chroma' or 'weaviate' for persisted on disk
    :param use_openai_embedding: Whether to use OpenAI embeddings for vector db
    :param use_openai_model: Whether to use OpenAI model for use with vector db
    :param hf_embedding_model: Which HF embedding model to use for vector db
           Default is instructor-large with 768 parameters per embedding if have GPUs, else all-MiniLM-L6-v1 if no GPUs
           Can also choose simpler model with 384 parameters per embedding: "sentence-transformers/all-MiniLM-L6-v2"
           Can also choose even better embedding with 1024 parameters: 'hkunlp/instructor-xl'
           We support automatically changing of embeddings for chroma, with a backup of db made if this is done
    :param allow_upload_to_user_data: Whether to allow file uploads to update shared vector db
    :param allow_upload_to_my_data: Whether to allow file uploads to update scratch vector db
    :param enable_url_upload: Whether to allow upload from URL
    :param enable_text_upload: Whether to allow upload of text
    :param enable_sources_list: Whether to allow list (or download for non-shared db) of list of sources for chosen db
    :param chunk: Whether to chunk data (True unless know data is already optimally chunked)
    :param chunk_size: Size of chunks, with typically top-4 passed to LLM, so neesd to be in context length
    :param top_k_docs: number of chunks to give LLM
    :param n_jobs: Number of processors to use when consuming documents (-1 = all, is default)
    :param enable_captions: Whether to support captions using BLIP for image files as documents, then preloads that model
    :param captions_model: Which model to use for captions.
           captions_model: str = "Salesforce/blip-image-captioning-base",  # continue capable
           captions_model: str = "Salesforce/blip2-flan-t5-xl",   # question/answer capable, 16GB state
           captions_model: str = "Salesforce/blip2-flan-t5-xxl",  # question/answer capable, 60GB state
           Note: opt-based blip2 are not permissive license due to opt and Meta license restrictions
    :param pre_load_caption_model: Whether to preload caption model, or load after forking parallel doc loader
           parallel loading disabled if preload and have images, to prevent deadlocking on cuda context
           Recommended if using larger caption model
    :param caption_gpu: If support caption, then use GPU if exists
    :param enable_ocr: Whether to support OCR on images
    :return:
    """
    is_hf = bool(int(os.getenv("HUGGINGFACE_SPACES", '0')))
    is_gpth2oai = bool(int(os.getenv("GPT_H2O_AI", '0')))
    is_public = is_hf or is_gpth2oai  # multi-user case with fixed model and disclaimer
    if memory_restriction_level is None:
        memory_restriction_level = 2 if is_hf else 0  # 2 assumes run on 24GB consumer GPU
    else:
        assert 0 <= memory_restriction_level <= 3, "Bad memory_restriction_level=%s" % memory_restriction_level
    admin_pass = os.getenv("ADMIN_PASS")
    # will sometimes appear in UI or sometimes actual generation, but maybe better than empty result
    # but becomes unrecoverable sometimes if raise, so just be silent for now
    raise_generate_gpu_exceptions = True

    # allow set token directly
    use_auth_token = os.environ.get("HUGGINGFACE_API_TOKEN", use_auth_token)
    allow_upload_to_user_data = bool(
        int(os.environ.get("allow_upload_to_user_data", str(int(allow_upload_to_user_data)))))
    allow_upload_to_my_data = bool(int(os.environ.get("allow_upload_to_my_data", str(int(allow_upload_to_my_data)))))
    height = int(os.environ.get("HEIGHT", height))
    h2ocolors = bool(int(os.getenv('h2ocolors', h2ocolors)))

    # allow enabling langchain via ENV
    # FIRST PLACE where LangChain referenced, but no imports related to it
    langchain_mode = os.environ.get("LANGCHAIN_MODE", langchain_mode)
    assert langchain_mode in langchain_modes, "Invalid langchain_mode %s" % langchain_mode
    visible_langchain_modes = ast.literal_eval(os.environ.get("visible_langchain_modes", str(visible_langchain_modes)))
    if langchain_mode not in visible_langchain_modes and langchain_mode in langchain_modes:
        visible_langchain_modes += [langchain_mode]

    if is_public:
        allow_upload_to_user_data = False
        input_lines = 1  # ensure set, for ease of use
        temperature = 0.2 if temperature is None else temperature
        top_p = 0.85 if top_p is None else top_p
        top_k = 70 if top_k is None else top_k
        if is_hf:
            do_sample = True if do_sample is None else do_sample
        else:
            # by default don't sample, too chatty
            do_sample = False if do_sample is None else do_sample

        if memory_restriction_level == 2:
            if not base_model:
                base_model = 'h2oai/h2ogpt-oasst1-512-12b'
                # don't set load_8bit if passed base_model, doesn't always work so can't just override
                load_8bit = True
                load_4bit = False  # FIXME - consider using 4-bit instead of 8-bit
        else:
            base_model = 'h2oai/h2ogpt-oasst1-512-20b' if not base_model else base_model
    if memory_restriction_level >= 2:
        load_8bit = True
        load_4bit = False  # FIXME - consider using 4-bit instead of 8-bit
        if hf_embedding_model is None:
            hf_embedding_model = "sentence-transformers/all-MiniLM-L6-v2"
    user_set_max_new_tokens = max_new_tokens is not None
    if is_public:
        if not max_time:
            max_time = 60 * 2
        if not max_max_time:
            max_max_time = max_time
        if not max_new_tokens:
            max_new_tokens = 256
        if not max_max_new_tokens:
            max_max_new_tokens = 256
    else:
        if not max_max_time:
            max_max_time = 60 * 20
        if not max_max_new_tokens:
            max_max_new_tokens = 256
    if is_hf:
        # must override share if in spaces
        share = False
        if not max_time:
            max_time = 60 * 1
        if not max_max_time:
            max_max_time = max_time
        # HF accounted for later in get_max_max_new_tokens()
    save_dir = os.getenv('SAVE_DIR', save_dir)
    score_model = os.getenv('SCORE_MODEL', score_model)
    if score_model == 'None' or score_model is None:
        score_model = ''
    concurrency_count = int(os.getenv('CONCURRENCY_COUNT', concurrency_count))
    api_open = bool(int(os.getenv('API_OPEN', str(int(api_open)))))
    allow_api = bool(int(os.getenv('ALLOW_API', str(int(allow_api)))))

    n_gpus = torch.cuda.device_count() if torch.cuda.is_available else 0
    if n_gpus == 0:
        gpu_id = None
        load_8bit = False
        load_4bit = False
        load_half = False
        infer_devices = False
        torch.backends.cudnn.benchmark = True
        torch.backends.cudnn.enabled = False
        torch.set_default_dtype(torch.float32)
        if psutil.virtual_memory().available < 94 * 1024 ** 3:
            # 12B uses ~94GB
            # 6.9B uses ~47GB
            base_model = 'h2oai/h2ogpt-oig-oasst1-512-6_9b' if not base_model else base_model
        if hf_embedding_model is None:
            # if no GPUs, use simpler embedding model to avoid cost in time
            hf_embedding_model = "sentence-transformers/all-MiniLM-L6-v2"
    else:
        if hf_embedding_model is None:
            # if still None, then set default
            hf_embedding_model = 'hkunlp/instructor-large'

    # get defaults
    model_lower = base_model.lower()
    if not gradio:
        # force, else not single response like want to look at
        stream_output = False
        # else prompt removal can mess up output
        chat = False
    # hard-coded defaults
    first_para = False
    text_limit = None

    if offload_folder:
        makedirs(offload_folder)
    if user_path:
        makedirs(user_path)

    placeholder_instruction, placeholder_input, \
        stream_output, show_examples, \
        prompt_type, prompt_dict, \
        temperature, top_p, top_k, num_beams, \
        max_new_tokens, min_new_tokens, early_stopping, max_time, \
        repetition_penalty, num_return_sequences, \
        do_sample, \
        src_lang, tgt_lang, \
        examples, \
        task_info = \
        get_generate_params(model_lower, chat,
                            stream_output, show_examples,
                            prompt_type, prompt_dict,
                            temperature, top_p, top_k, num_beams,
                            max_new_tokens, min_new_tokens, early_stopping, max_time,
                            repetition_penalty, num_return_sequences,
                            do_sample,
                            top_k_docs,
                            chunk,
                            chunk_size,
                            verbose,
                            )

    locals_dict = locals()
    locals_print = '\n'.join(['%s: %s' % (k, v) for k, v in locals_dict.items()])
    if verbose:
        print(f"Generating model with params:\n{locals_print}", flush=True)
        print("Command: %s\nHash: %s" % (str(' '.join(sys.argv)), get_githash()), flush=True)

    if langchain_mode != "Disabled":
        # SECOND PLACE where LangChain referenced, but all imports are kept local so not required
        from gpt_langchain import prep_langchain, get_some_dbs_from_hf
        if is_hf:
            get_some_dbs_from_hf()
        dbs = {}
        for langchain_mode1 in visible_langchain_modes:
            if langchain_mode1 in ['MyData']:
                # don't use what is on disk, remove it instead
                for gpath1 in glob.glob(os.path.join(scratch_base_dir, 'db_dir_%s*' % langchain_mode1)):
                    if os.path.isdir(gpath1):
                        print("Removing old MyData: %s" % gpath1, flush=True)
                        shutil.rmtree(gpath1)
                continue
            if langchain_mode1 in ['All']:
                # FIXME: All should be avoided until scans over each db, shouldn't be separate db
                continue
            persist_directory1 = 'db_dir_%s' % langchain_mode1  # single place, no special names for each case
            try:
                db = prep_langchain(persist_directory1,
                                    load_db_if_exists,
                                    db_type, use_openai_embedding,
                                    langchain_mode1, user_path,
                                    hf_embedding_model,
                                    kwargs_make_db=locals())
            finally:
                # in case updated embeddings or created new embeddings
                clear_torch_cache()
            dbs[langchain_mode1] = db
        # remove None db's so can just rely upon k in dbs for if hav db
        dbs = {k: v for k, v in dbs.items() if v is not None}
    else:
        dbs = {}
        # import control
        if os.environ.get("TEST_LANGCHAIN_IMPORT"):
            assert 'gpt_langchain' not in sys.modules, "Dev bug, import of langchain when should not have"
            assert 'langchain' not in sys.modules, "Dev bug, import of langchain when should not have"

    if cli:
        from cli import run_cli
        return run_cli(**get_kwargs(run_cli, exclude_names=['model_state0'], **locals()))
    elif not gradio:
        from eval import run_eval
        return run_eval(**get_kwargs(run_eval, exclude_names=['model_state0'], **locals()))
    elif gradio:
        # imported here so don't require gradio to run generate
        from gradio_runner import go_gradio

        # get default model
        all_kwargs = locals().copy()
        if all_kwargs.get('base_model') and not all_kwargs['login_mode_if_model0']:
            model0, tokenizer0, device = get_model(reward_type=False,
                                                   **get_kwargs(get_model, exclude_names=['reward_type'], **all_kwargs))
        else:
            # if empty model, then don't load anything, just get gradio up
            model0, tokenizer0, device = None, None, None
        model_state0 = [model0, tokenizer0, device, all_kwargs['base_model']]

        # get score model
        smodel, stokenizer, sdevice = get_score_model(reward_type=True,
                                                      **get_kwargs(get_score_model, exclude_names=['reward_type'],
                                                                   **all_kwargs))
        score_model_state0 = [smodel, stokenizer, sdevice, score_model]

        if enable_captions:
            if pre_load_caption_model:
                from image_captions import H2OImageCaptionLoader
                caption_loader = H2OImageCaptionLoader(caption_gpu=caption_gpu).load_model()
            else:
                caption_loader = 'gpu' if caption_gpu else 'cpu'
        else:
            caption_loader = False

        # assume gradio needs everything
        go_gradio(**locals())


def get_non_lora_model(base_model, model_loader, load_half, model_kwargs, reward_type,
                       gpu_id=0,
                       use_auth_token=False,
                       trust_remote_code=True,
                       offload_folder=None,
                       triton_attn=False,
                       long_sequence=True,
                       ):
    """
    Ensure model gets on correct device
    :param base_model:
    :param model_loader:
    :param load_half:
    :param model_kwargs:
    :param reward_type:
    :param gpu_id:
    :param use_auth_token:
    :param trust_remote_code:
    :param offload_folder:
    :param triton_attn:
    :param long_sequence:
    :return:
    """
    with init_empty_weights():
        from transformers import AutoConfig
        config = AutoConfig.from_pretrained(base_model, use_auth_token=use_auth_token,
                                            trust_remote_code=trust_remote_code,
                                            offload_folder=offload_folder)
        if triton_attn and 'mpt-' in base_model.lower():
            config.attn_config['attn_impl'] = 'triton'
        if long_sequence:
            if 'mpt-7b-storywriter' in base_model.lower():
                config.update({"max_seq_len": 83968})
            if 'mosaicml/mpt-7b-chat' in base_model.lower():
                config.update({"max_seq_len": 4096})
        if issubclass(config.__class__, tuple(AutoModel._model_mapping.keys())):
            model = AutoModel.from_config(
                config,
                trust_remote_code=trust_remote_code,
            )
        else:
            # can't infer
            model = None

    if model is not None:
        # NOTE: Can specify max_memory={0: max_mem, 1: max_mem}, to shard model
        # NOTE: Some models require avoiding sharding some layers,
        # then would pass no_split_module_classes and give list of those layers.
        device_map = infer_auto_device_map(
            model,
            dtype=torch.float16 if load_half else torch.float32,
        )
        if hasattr(model, 'model'):
            device_map_model = infer_auto_device_map(
                model.model,
                dtype=torch.float16 if load_half else torch.float32,
            )
            device_map.update(device_map_model)
    else:
        device_map = "auto"

    n_gpus = torch.cuda.device_count() if torch.cuda.is_available else 0

    if n_gpus > 0:
        if gpu_id >= 0:
            # FIXME: If really distributes model, tend to get things like: ValueError: gpt_neox.embed_in.weight doesn't have any device set.
            # So avoid for now, just put on first GPU, unless score_model, put on last
            if reward_type:
                device_map = {'': n_gpus - 1}
            else:
                device_map = {'': min(n_gpus - 1, gpu_id)}
        if gpu_id == -1:
            device_map = {'': 'cuda'}
    else:
        device_map = {'': 'cpu'}
        model_kwargs['load_in_8bit'] = False
        model_kwargs['load_in_4bit'] = False
    print('device_map: %s' % device_map, flush=True)

    load_in_8bit = model_kwargs.get('load_in_8bit', False)
    load_in_4bit = model_kwargs.get('load_in_4bit', False)
    model_kwargs['device_map'] = device_map
    pop_unused_model_kwargs(model_kwargs)

    if load_in_8bit or load_in_4bit or not load_half:
        model = model_loader.from_pretrained(
            base_model,
            config=config,
            **model_kwargs,
        )
    else:
        model = model_loader.from_pretrained(
            base_model,
            config=config,
            **model_kwargs,
        ).half()
    return model


def get_model(
        load_8bit: bool = False,
        load_4bit: bool = False,
        load_half: bool = True,
        infer_devices: bool = True,
        base_model: str = '',
        tokenizer_base_model: str = '',
        lora_weights: str = "",
        gpu_id: int = 0,

        reward_type: bool = None,
        local_files_only: bool = False,
        resume_download: bool = True,
        use_auth_token: Union[str, bool] = False,
        trust_remote_code: bool = True,
        offload_folder: str = None,
        compile_model: bool = True,

        verbose: bool = False,
):
    """

    :param load_8bit: load model in 8-bit, not supported by all models
    :param load_4bit: load model in 4-bit, not supported by all models
    :param load_half: load model in 16-bit
    :param infer_devices: Use torch infer of optimal placement of layers on devices (for non-lora case)
           For non-LORA case, False will spread shards across multiple GPUs, but this can lead to cuda:x cuda:y mismatches
           So it is not the default
    :param base_model: name/path of base model
    :param tokenizer_base_model: name/path of tokenizer
    :param lora_weights: name/path
    :param gpu_id: which GPU (0..n_gpus-1) or allow all GPUs if relevant (-1)
    :param reward_type: reward type model for sequence classification
    :param local_files_only: use local files instead of from HF
    :param resume_download: resume downloads from HF
    :param use_auth_token: assumes user did on CLI `huggingface-cli login` to access private repo
    :param trust_remote_code: trust code needed by model
    :param offload_folder: offload folder
    :param compile_model: whether to compile torch model
    :param verbose:
    :return:
    """
    if verbose:
        print("Get %s model" % base_model, flush=True)
    if base_model in non_hf_types:
        from gpt4all_llm import get_model_tokenizer_gpt4all
        model, tokenizer, device = get_model_tokenizer_gpt4all(base_model)
        return model, tokenizer, device

    if lora_weights is not None and lora_weights.strip():
        if verbose:
            print("Get %s lora weights" % lora_weights, flush=True)
    device = get_device()

    if 'gpt2' in base_model.lower():
        # RuntimeError: where expected condition to be a boolean tensor, but got a tensor with dtype Half
        load_8bit = False
        load_4bit = False

    assert base_model.strip(), (
        "Please choose a base model with --base_model (CLI) or load one from Models Tab (gradio)"
    )

    from transformers import AutoConfig
    config = AutoConfig.from_pretrained(base_model, use_auth_token=use_auth_token,
                                        trust_remote_code=trust_remote_code,
                                        offload_folder=offload_folder)
    llama_type_from_config = 'llama' in str(config).lower()
    llama_type_from_name = "llama" in base_model.lower()
    llama_type = llama_type_from_config or llama_type_from_name
    if llama_type:
        if verbose:
            print("Detected as llama type from"
                  " config (%s) or name (%s)" % (llama_type_from_config, llama_type_from_name), flush=True)

    model_loader, tokenizer_loader = get_loaders(llama_type=llama_type, model_name=base_model, reward_type=reward_type)
    if not tokenizer_base_model:
        tokenizer_base_model = base_model

    if tokenizer_loader is not None and not isinstance(tokenizer_loader, str):
        tokenizer = tokenizer_loader.from_pretrained(tokenizer_base_model,
                                                     local_files_only=local_files_only,
                                                     resume_download=resume_download,
                                                     use_auth_token=use_auth_token,
                                                     trust_remote_code=trust_remote_code,
                                                     offload_folder=offload_folder,
                                                     padding_side='left',
                                                     )
    else:
        tokenizer = tokenizer_loader

    if isinstance(tokenizer, str):
        # already a pipeline, tokenizer_loader is string for task
        model = model_loader(tokenizer,
                             model=base_model,
                             device=0 if device == "cuda" else -1,
                             torch_dtype=torch.float16 if device == 'cuda' else torch.float32)
    else:
        assert device in ["cuda", "cpu"], "Unsupported device %s" % device
        model_kwargs = dict(local_files_only=local_files_only,
                            torch_dtype=torch.float16 if device == 'cuda' else torch.float32,
                            resume_download=resume_download,
                            use_auth_token=use_auth_token,
                            trust_remote_code=trust_remote_code,
                            offload_folder=offload_folder,
                            )
        if 'mbart-' not in base_model.lower() and 'mpt-' not in base_model.lower():
            model_kwargs.update(dict(load_in_8bit=load_8bit,
                                     load_in_4bit=load_4bit,
                                     device_map={"": 0} if (load_8bit or load_4bit) and device == 'cuda' else "auto",
                                     ))
        if 'mpt-' in base_model.lower() and gpu_id >= 0:
            model_kwargs.update(dict(device_map={"": gpu_id} if device == 'cuda' else "cpu"))

        if 'OpenAssistant/reward-model'.lower() in base_model.lower():
            # FIXME: could put on other GPUs
            model_kwargs['device_map'] = {"": 0} if device == 'cuda' else {"": 'cpu'}
            model_kwargs.pop('torch_dtype', None)
        pop_unused_model_kwargs(model_kwargs)

        if not lora_weights:
            with torch.device(device):
                if infer_devices:
                    model = get_non_lora_model(base_model, model_loader, load_half, model_kwargs, reward_type,
                                               gpu_id=gpu_id,
                                               use_auth_token=use_auth_token,
                                               trust_remote_code=trust_remote_code,
                                               offload_folder=offload_folder,
                                               )
                else:
                    if load_half and not (load_8bit or load_4bit):
                        model = model_loader.from_pretrained(
                            base_model,
                            **model_kwargs).half()
                    else:
                        model = model_loader.from_pretrained(
                            base_model,
                            **model_kwargs)
        elif load_8bit or load_4bit:
            model = model_loader.from_pretrained(
                base_model,
                **model_kwargs
            )
            from peft import PeftModel  # loads cuda, so avoid in global scope
            model = PeftModel.from_pretrained(
                model,
                lora_weights,
                torch_dtype=torch.float16 if device == 'cuda' else torch.float32,
                local_files_only=local_files_only,
                resume_download=resume_download,
                use_auth_token=use_auth_token,
                trust_remote_code=trust_remote_code,
                offload_folder=offload_folder,
                device_map={"": 0} if device == 'cuda' else {"": 'cpu'},  # seems to be required
            )
        else:
            with torch.device(device):
                model = model_loader.from_pretrained(
                    base_model,
                    **model_kwargs
                )
                from peft import PeftModel  # loads cuda, so avoid in global scope
                model = PeftModel.from_pretrained(
                    model,
                    lora_weights,
                    torch_dtype=torch.float16 if device == 'cuda' else torch.float32,
                    local_files_only=local_files_only,
                    resume_download=resume_download,
                    use_auth_token=use_auth_token,
                    trust_remote_code=trust_remote_code,
                    offload_folder=offload_folder,
                    device_map="auto",
                )
                if load_half:
                    model.half()

    # unwind broken decapoda-research config
    if llama_type:
        model.config.pad_token_id = tokenizer.pad_token_id = 0  # unk
        model.config.bos_token_id = 1
        model.config.eos_token_id = 2
    if 'gpt2' in base_model.lower():
        # add special tokens that otherwise all share the same id
        tokenizer.add_special_tokens({'bos_token': '<bos>',
                                      'eos_token': '<eos>',
                                      'pad_token': '<pad>'})

    if not isinstance(tokenizer, str):
        model.eval()
        if torch.__version__ >= "2" and sys.platform != "win32" and compile_model:
            model = torch.compile(model)

    if hasattr(config, 'max_seq_len') and isinstance(config.max_seq_len, int):
        tokenizer.model_max_length = config.max_seq_len
    elif hasattr(config, 'max_position_embeddings') and isinstance(config.max_position_embeddings, int):
        # help automatically limit inputs to generate
        tokenizer.model_max_length = config.max_position_embeddings
    else:
        if verbose:
            print("Could not determine model_max_length, setting to 2048", flush=True)
        tokenizer.model_max_length = 2048

    return model, tokenizer, device


def pop_unused_model_kwargs(model_kwargs):
    """
    in-place pop unused kwargs that are not dependency-upgrade friendly
    no point passing in False, is default, and helps avoid needing to update requirements for new deps
    :param model_kwargs:
    :return:
    """
    check_list = ['load_in_8bit', 'load_in_4bit']
    for k in check_list:
        if k in model_kwargs and not model_kwargs[k]:
            model_kwargs.pop(k)


def get_score_model(score_model: str = None,
                    load_8bit: bool = False,
                    load_4bit: bool = False,
                    load_half: bool = True,
                    infer_devices: bool = True,
                    base_model: str = '',
                    tokenizer_base_model: str = '',
                    lora_weights: str = "",
                    gpu_id: int = 0,

                    reward_type: bool = None,
                    local_files_only: bool = False,
                    resume_download: bool = True,
                    use_auth_token: Union[str, bool] = False,
                    trust_remote_code: bool = True,
                    offload_folder: str = None,
                    compile_model: bool = True,

                    verbose: bool = False,
                    ):
    if score_model is not None and score_model.strip():
        load_8bit = False
        load_4bit = False
        load_half = False
        base_model = score_model.strip()
        tokenizer_base_model = ''
        lora_weights = ''
        llama_type = False
        compile_model = False
        smodel, stokenizer, sdevice = get_model(reward_type=True,
                                                **get_kwargs(get_model, exclude_names=['reward_type'], **locals()))
    else:
        smodel, stokenizer, sdevice = None, None, None
    return smodel, stokenizer, sdevice


no_default_param_names = [
    'instruction',
    'iinput',
    'context',
    'instruction_nochat',
    'iinput_nochat',
]

gen_hyper = ['temperature',
             'top_p',
             'top_k',
             'num_beams',
             'max_new_tokens',
             'min_new_tokens',
             'early_stopping',
             'max_time',
             'repetition_penalty',
             'num_return_sequences',
             'do_sample',
             ]

eval_func_param_names = ['instruction',
                         'iinput',
                         'context',
                         'stream_output',
                         'prompt_type',
                         'prompt_dict'] + \
                        gen_hyper + \
                        ['chat',
                         'instruction_nochat',
                         'iinput_nochat',
                         'langchain_mode',
                         'top_k_docs',
                         'chunk',
                         'chunk_size',
                         'document_choice',
                         ]

# form evaluate defaults for submit_nochat_api
eval_func_param_names_defaults = eval_func_param_names.copy()
for k in no_default_param_names:
    if k in eval_func_param_names_defaults:
        eval_func_param_names_defaults.remove(k)


def evaluate_from_str(
        model_state,
        my_db_state,
        # START NOTE: Examples must have same order of parameters
        user_kwargs,
        # END NOTE: Examples must have same order of parameters
        default_kwargs=None,
        src_lang=None,
        tgt_lang=None,
        debug=False,
        concurrency_count=None,
        save_dir=None,
        sanitize_bot_response=False,
        model_state0=None,
        memory_restriction_level=None,
        raise_generate_gpu_exceptions=None,
        chat_context=None,
        lora_weights=None,
        load_db_if_exists=True,
        dbs=None,
        user_path=None,
        detect_user_path_changes_every_query=None,
        use_openai_embedding=None,
        use_openai_model=None,
        hf_embedding_model=None,
        chunk=None,
        chunk_size=None,
        db_type=None,
        n_jobs=None,
        first_para=None,
        text_limit=None,
        verbose=False,
        cli=False,
):
    if isinstance(user_kwargs, str):
        user_kwargs = ast.literal_eval(user_kwargs)
    # only used for submit_nochat_api
    user_kwargs['chat'] = False
    user_kwargs['stream_output'] = False
    if 'langchain_mode' not in user_kwargs:
        # if user doesn't specify, then assume disabled, not use default
        user_kwargs['langchain_mode'] = 'Disabled'

    assert set(list(default_kwargs.keys())) == set(eval_func_param_names)
    # correct ordering.  Note some things may not be in default_kwargs, so can't be default of user_kwargs.get()
    args_list = [user_kwargs[k] if k in user_kwargs else default_kwargs[k] for k in eval_func_param_names]

    ret = evaluate(
        model_state,
        my_db_state,
        # START NOTE: Examples must have same order of parameters
        *tuple(args_list),
        # END NOTE: Examples must have same order of parameters
        src_lang=src_lang,
        tgt_lang=tgt_lang,
        debug=debug,
        concurrency_count=concurrency_count,
        save_dir=save_dir,
        sanitize_bot_response=sanitize_bot_response,
        model_state0=model_state0,
        memory_restriction_level=memory_restriction_level,
        raise_generate_gpu_exceptions=raise_generate_gpu_exceptions,
        chat_context=chat_context,
        lora_weights=lora_weights,
        load_db_if_exists=load_db_if_exists,
        dbs=dbs,
        user_path=user_path,
        detect_user_path_changes_every_query=detect_user_path_changes_every_query,
        use_openai_embedding=use_openai_embedding,
        use_openai_model=use_openai_model,
        hf_embedding_model=hf_embedding_model,
        db_type=db_type,
        n_jobs=n_jobs,
        first_para=first_para,
        text_limit=text_limit,
        verbose=verbose,
        cli=cli,
    )
    try:
        for ret1 in ret:
            yield ret1
    finally:
        # clear before return, in finally in case GPU OOM exception
        clear_torch_cache()


def evaluate(
        model_state,
        my_db_state,
        # START NOTE: Examples must have same order of parameters
        instruction,
        iinput,
        context,
        stream_output,
        prompt_type,
        prompt_dict,
        temperature,
        top_p,
        top_k,
        num_beams,
        max_new_tokens,
        min_new_tokens,
        early_stopping,
        max_time,
        repetition_penalty,
        num_return_sequences,
        do_sample,
        chat,
        instruction_nochat,
        iinput_nochat,
        langchain_mode,
        top_k_docs,
        chunk,
        chunk_size,
        document_choice,
        # END NOTE: Examples must have same order of parameters
        src_lang=None,
        tgt_lang=None,
        debug=False,
        concurrency_count=None,
        save_dir=None,
        sanitize_bot_response=False,
        model_state0=None,
        memory_restriction_level=None,
        raise_generate_gpu_exceptions=None,
        chat_context=None,
        lora_weights=None,
        load_db_if_exists=True,
        dbs=None,
        user_path=None,
        detect_user_path_changes_every_query=None,
        use_openai_embedding=None,
        use_openai_model=None,
        hf_embedding_model=None,
        db_type=None,
        n_jobs=None,
        first_para=None,
        text_limit=None,
        verbose=False,
        cli=False,
):
    # ensure passed these
    assert concurrency_count is not None
    assert memory_restriction_level is not None
    assert raise_generate_gpu_exceptions is not None
    assert chat_context is not None
    assert use_openai_embedding is not None
    assert use_openai_model is not None
    assert hf_embedding_model is not None
    assert db_type is not None
    assert top_k_docs is not None and isinstance(top_k_docs, int)
    assert chunk is not None and isinstance(chunk, bool)
    assert chunk_size is not None and isinstance(chunk_size, int)
    assert n_jobs is not None
    assert first_para is not None

    if debug:
        locals_dict = locals().copy()
        locals_dict.pop('model_state', None)
        locals_dict.pop('model_state0', None)
        print(locals_dict)

    no_model_msg = "Please choose a base model with --base_model (CLI) or load in Models Tab (gradio).\nThen start New Conversation"

    if model_state0 is None:
        # e.g. for no gradio case, set dummy value, else should be set
        model_state0 = [None, None, None, None]

    if model_state is not None and len(model_state) == 4 and not isinstance(model_state[0], str):
        # try to free-up original model (i.e. list was passed as reference)
        if model_state0 is not None and model_state0[0] is not None:
            model_state0[0].cpu()
            model_state0[0] = None
        # try to free-up original tokenizer (i.e. list was passed as reference)
        if model_state0 is not None and model_state0[1] is not None:
            model_state0[1] = None
        clear_torch_cache()
        model, tokenizer, device, base_model = model_state
    elif model_state0 is not None and len(model_state0) == 4 and model_state0[0] is not None:
        assert isinstance(model_state[0], str)
        model, tokenizer, device, base_model = model_state0
    else:
        raise AssertionError(no_model_msg)

    if base_model is None:
        raise AssertionError(no_model_msg)

    assert base_model.strip(), no_model_msg
    assert model, "Model is missing"
    assert tokenizer, "Tokenizer is missing"

    # choose chat or non-chat mode
    if not chat:
        instruction = instruction_nochat
        iinput = iinput_nochat

    if not context:
        # get hidden context if have one
        context = get_context(chat_context, prompt_type)

    prompter = Prompter(prompt_type, prompt_dict, debug=debug, chat=chat, stream_output=stream_output)
    data_point = dict(context=context, instruction=instruction, input=iinput)
    prompt = prompter.generate_prompt(data_point)

    # THIRD PLACE where LangChain referenced, but imports only occur if enabled and have db to use
    assert langchain_mode in langchain_modes, "Invalid langchain_mode %s" % langchain_mode
    if langchain_mode in ['MyData'] and my_db_state is not None and len(my_db_state) > 0 and my_db_state[0] is not None:
        db1 = my_db_state[0]
    elif dbs is not None and langchain_mode in dbs:
        db1 = dbs[langchain_mode]
    else:
        db1 = None
    if langchain_mode not in [False, 'Disabled', 'ChatLLM', 'LLM'] and db1 is not None or base_model in non_hf_types:
        query = instruction if not iinput else "%s\n%s" % (instruction, iinput)
        outr = ""
        # use smaller cut_distanct for wiki_full since so many matches could be obtained, and often irrelevant unless close
        from gpt_langchain import run_qa_db
        for r in run_qa_db(query=query,
                           model_name=base_model, model=model, tokenizer=tokenizer,
                           stream_output=stream_output,
                           prompter=prompter,
                           load_db_if_exists=load_db_if_exists,
                           db=db1,
                           user_path=user_path,
                           detect_user_path_changes_every_query=detect_user_path_changes_every_query,
                           cut_distanct=1.1 if langchain_mode in ['wiki_full'] else 1.64,  # FIXME, too arbitrary
                           use_openai_embedding=use_openai_embedding,
                           use_openai_model=use_openai_model,
                           hf_embedding_model=hf_embedding_model,
                           first_para=first_para,
                           text_limit=text_limit,
                           chunk=chunk,
                           chunk_size=chunk_size,
                           langchain_mode=langchain_mode,
                           document_choice=document_choice,
                           db_type=db_type,
                           top_k_docs=top_k_docs,

                           # gen_hyper:
                           do_sample=do_sample,
                           temperature=temperature,
                           repetition_penalty=repetition_penalty,
                           top_k=top_k,
                           top_p=top_p,
                           num_beams=num_beams,
                           min_new_tokens=min_new_tokens,
                           max_new_tokens=max_new_tokens,
                           early_stopping=early_stopping,
                           max_time=max_time,
                           num_return_sequences=num_return_sequences,

                           prompt_type=prompt_type,
                           prompt_dict=prompt_dict,
                           n_jobs=n_jobs,
                           verbose=verbose,
                           cli=cli,
                           ):
            outr, extra = r  # doesn't accumulate, new answer every yield, so only save that full answer
            yield dict(response=outr, sources=extra)
        if save_dir:
            save_generate_output(output=outr, base_model=base_model, save_dir=save_dir)
            if verbose:
                print(
                    'Post-Generate Langchain: %s decoded_output: %s' % (str(datetime.now()), len(outr) if outr else -1),
                    flush=True)
        if outr or base_model in non_hf_types:
            # if got no response (e.g. not showing sources and got no sources,
            # so nothing to give to LLM), then slip through and ask LLM
            # Or if llama/gptj, then just return since they had no response and can't go down below code path
            # clear before return, since .then() never done if from API
            clear_torch_cache()
            return

    if isinstance(tokenizer, str):
        # pipeline
        if tokenizer == "summarization":
            key = 'summary_text'
        else:
            raise RuntimeError("No such task type %s" % tokenizer)
        # NOTE: uses max_length only
        yield dict(response=model(prompt, max_length=max_new_tokens)[0][key], sources='')

    if 'mbart-' in base_model.lower():
        assert src_lang is not None
        tokenizer.src_lang = languages_covered()[src_lang]

    if chat:
        # override, ignore user change
        num_return_sequences = 1
    stopping_criteria = get_stopping(prompt_type, prompt_dict, tokenizer, device,
                                     model_max_length=tokenizer.model_max_length)

    # limit prompt using token length from user, implicit, or model
    _, _, max_length_tokenize, max_prompt_length = get_cutoffs(memory_restriction_level,
                                                               model_max_length=tokenizer.model_max_length)
    from h2oai_pipeline import H2OTextGenerationPipeline
    prompt = H2OTextGenerationPipeline.limit_prompt(prompt, tokenizer, max_prompt_length=max_prompt_length)

    inputs = tokenizer(prompt, return_tensors="pt")
    if debug and len(inputs["input_ids"]) > 0:
        print('input_ids length', len(inputs["input_ids"][0]), flush=True)
    input_ids = inputs["input_ids"].to(device)
    # CRITICAL LIMIT else will fail
    max_max_tokens = tokenizer.model_max_length
    max_input_tokens = max_max_tokens - max_new_tokens
    input_ids = input_ids[:, -max_input_tokens:]
    gen_config_kwargs = dict(temperature=float(temperature),
                             top_p=float(top_p),
                             top_k=top_k,
                             num_beams=num_beams,
                             do_sample=do_sample,
                             repetition_penalty=float(repetition_penalty),
                             num_return_sequences=num_return_sequences,
                             renormalize_logits=True,
                             remove_invalid_values=True,
                             )
    token_ids = ['eos_token_id', 'pad_token_id', 'bos_token_id', 'cls_token_id', 'sep_token_id']
    for token_id in token_ids:
        if hasattr(tokenizer, token_id) and getattr(tokenizer, token_id) is not None:
            gen_config_kwargs.update({token_id: getattr(tokenizer, token_id)})
    generation_config = GenerationConfig(**gen_config_kwargs)

    gen_kwargs = dict(input_ids=input_ids,
                      generation_config=generation_config,
                      return_dict_in_generate=True,
                      output_scores=True,
                      max_new_tokens=max_new_tokens,  # prompt + new
                      min_new_tokens=min_new_tokens,  # prompt + new
                      early_stopping=early_stopping,  # False, True, "never"
                      max_time=max_time,
                      stopping_criteria=stopping_criteria,
                      )
    if 'gpt2' in base_model.lower():
        gen_kwargs.update(dict(bos_token_id=tokenizer.bos_token_id, pad_token_id=tokenizer.eos_token_id))
    elif 'mbart-' in base_model.lower():
        assert tgt_lang is not None
        tgt_lang = languages_covered()[tgt_lang]
        gen_kwargs.update(dict(forced_bos_token_id=tokenizer.lang_code_to_id[tgt_lang]))
    else:
        token_ids = ['eos_token_id', 'bos_token_id', 'pad_token_id']
        for token_id in token_ids:
            if hasattr(tokenizer, token_id) and getattr(tokenizer, token_id) is not None:
                gen_kwargs.update({token_id: getattr(tokenizer, token_id)})

    decoder_kwargs = dict(skip_special_tokens=True,
                          clean_up_tokenization_spaces=True)

    decoder = functools.partial(tokenizer.decode,
                                **decoder_kwargs
                                )
    decoder_raw_kwargs = dict(skip_special_tokens=False,
                              clean_up_tokenization_spaces=True)

    decoder_raw = functools.partial(tokenizer.decode,
                                    **decoder_raw_kwargs
                                    )

    with torch.no_grad():
        context_class_cast = NullContext if device == 'cpu' or lora_weights else torch.autocast
        with context_class_cast(device):
            # protection for gradio not keeping track of closed users,
            # else hit bitsandbytes lack of thread safety:
            # https://github.com/h2oai/h2ogpt/issues/104
            # but only makes sense if concurrency_count == 1
            context_class = NullContext  # if concurrency_count > 1 else filelock.FileLock
            if verbose:
                print('Pre-Generate: %s' % str(datetime.now()), flush=True)
            decoded_output = None
            with context_class("generate.lock"):
                if verbose:
                    print('Generate: %s' % str(datetime.now()), flush=True)
                # decoded tokenized prompt can deviate from prompt due to special characters
                inputs_decoded = decoder(input_ids[0])
                inputs_decoded_raw = decoder_raw(input_ids[0])
                if inputs_decoded == prompt:
                    # normal
                    pass
                elif inputs_decoded.lstrip() == prompt.lstrip():
                    # sometimes extra space in front, make prompt same for prompt removal
                    prompt = inputs_decoded
                elif inputs_decoded_raw == prompt:
                    # some models specify special tokens that are part of normal prompt, so can't skip them
                    inputs_decoded = prompt = inputs_decoded_raw
                    decoder = decoder_raw
                    decoder_kwargs = decoder_raw_kwargs
                elif inputs_decoded_raw.replace("<unk> ", "").replace("<unk>", "").replace('\n', ' ').replace(' ',
                                                                                                              '') == prompt.replace(
                    '\n', ' ').replace(' ', ''):
                    inputs_decoded = prompt = inputs_decoded_raw
                    decoder = decoder_raw
                    decoder_kwargs = decoder_raw_kwargs
                else:
                    if verbose:
                        print("WARNING: Special characters in prompt", flush=True)
                if stream_output:
                    skip_prompt = False
                    streamer = H2OTextIteratorStreamer(tokenizer, skip_prompt=skip_prompt, block=False,
                                                       **decoder_kwargs)
                    gen_kwargs.update(dict(streamer=streamer))
                    target = wrapped_partial(generate_with_exceptions, model.generate,
                                             prompt=prompt, inputs_decoded=inputs_decoded,
                                             raise_generate_gpu_exceptions=raise_generate_gpu_exceptions,
                                             **gen_kwargs)
                    bucket = queue.Queue()
                    thread = EThread(target=target, streamer=streamer, bucket=bucket)
                    thread.start()
                    outputs = ""
                    try:
                        for new_text in streamer:
                            if bucket.qsize() > 0 or thread.exc:
                                thread.join()
                            outputs += new_text
                            yield dict(response=prompter.get_response(outputs, prompt=inputs_decoded,
                                                                      sanitize_bot_response=sanitize_bot_response),
                                       sources='')
                    except BaseException:
                        # if any exception, raise that exception if was from thread, first
                        if thread.exc:
                            raise thread.exc
                        raise
                    finally:
                        # clear before return, since .then() never done if from API
                        clear_torch_cache()
                        # in case no exception and didn't join with thread yet, then join
                        if not thread.exc:
                            thread.join()
                    # in case raise StopIteration or broke queue loop in streamer, but still have exception
                    if thread.exc:
                        raise thread.exc
                    decoded_output = outputs
                else:
                    try:
                        outputs = model.generate(**gen_kwargs)
                    finally:
                        clear_torch_cache()  # has to be here for API submit_nochat_api since.then() not called
                    outputs = [decoder(s) for s in outputs.sequences]
                    yield dict(response=prompter.get_response(outputs, prompt=inputs_decoded,
                                                              sanitize_bot_response=sanitize_bot_response), sources='')
                    if outputs and len(outputs) >= 1:
                        decoded_output = prompt + outputs[0]
                if save_dir and decoded_output:
                    save_generate_output(output=decoded_output, base_model=base_model, save_dir=save_dir)
            if verbose:
                print('Post-Generate: %s decoded_output: %s' % (
                    str(datetime.now()), len(decoded_output) if decoded_output else -1), flush=True)


inputs_list_names = list(inspect.signature(evaluate).parameters)
state_names = ['model_state', 'my_db_state']
inputs_kwargs_list = [x for x in inputs_list_names if x not in eval_func_param_names + state_names]


def get_cutoffs(memory_restriction_level, for_context=False, model_max_length=2048):
    # help to avoid errors like:
    # RuntimeError: The size of tensor a (2048) must match the size of tensor b (2049) at non-singleton dimension 3
    # RuntimeError: expected scalar type Half but found Float
    # with - 256
    if memory_restriction_level > 0:
        max_length_tokenize = 768 - 256 if memory_restriction_level <= 2 else 512 - 256
    else:
        # at least give room for 1 paragraph output
        max_length_tokenize = model_max_length - 256
    cutoff_len = max_length_tokenize * 4  # if reaches limit, then can't generate new tokens
    output_smallest = 30 * 4
    max_prompt_length = cutoff_len - output_smallest

    if for_context:
        # then lower even more to avoid later chop, since just estimate tokens in context bot
        max_prompt_length = max(64, int(max_prompt_length * 0.8))

    return cutoff_len, output_smallest, max_length_tokenize, max_prompt_length


class H2OTextIteratorStreamer(TextIteratorStreamer):
    """
    normally, timeout required for now to handle exceptions, else get()
    but with H2O version of TextIteratorStreamer, loop over block to handle
    """

    def __init__(self, tokenizer, skip_prompt: bool = False, timeout: typing.Optional[float] = None,
                 block=True, **decode_kwargs):
        super().__init__(tokenizer, skip_prompt, **decode_kwargs)
        self.text_queue = queue.Queue()
        self.stop_signal = None
        self.do_stop = False
        self.timeout = timeout
        self.block = block

    def on_finalized_text(self, text: str, stream_end: bool = False):
        """Put the new text in the queue. If the stream is ending, also put a stop signal in the queue."""
        self.text_queue.put(text, timeout=self.timeout)
        if stream_end:
            self.text_queue.put(self.stop_signal, timeout=self.timeout)

    def __iter__(self):
        return self

    def __next__(self):
        while True:
            try:
                value = self.stop_signal  # value looks unused in pycharm, not true
                if self.do_stop:
                    print("hit stop", flush=True)
                    # could raise or break, maybe best to raise and make parent see if any exception in thread
                    raise StopIteration()
                    # break
                value = self.text_queue.get(block=self.block, timeout=self.timeout)
                break
            except queue.Empty:
                time.sleep(0.01)
        if value == self.stop_signal:
            raise StopIteration()
        else:
            return value


def generate_with_exceptions(func, *args, prompt='', inputs_decoded='', raise_generate_gpu_exceptions=True, **kwargs):
    try:
        func(*args, **kwargs)
    except torch.cuda.OutOfMemoryError as e:
        print("GPU OOM 2: prompt: %s inputs_decoded: %s exception: %s" % (prompt, inputs_decoded, str(e)),
              flush=True)
        if 'input_ids' in kwargs:
            if kwargs['input_ids'] is not None:
                kwargs['input_ids'].cpu()
            kwargs['input_ids'] = None
        traceback.print_exc()
        clear_torch_cache()
        return
    except (Exception, RuntimeError) as e:
        if 'Expected all tensors to be on the same device' in str(e) or \
                'expected scalar type Half but found Float' in str(e) or \
                'probability tensor contains either' in str(e) or \
                'cublasLt ran into an error!' in str(e) or \
                'mat1 and mat2 shapes cannot be multiplied' in str(e):
            print(
                "GPU Error: prompt: %s inputs_decoded: %s exception: %s" % (prompt, inputs_decoded, str(e)),
                flush=True)
            traceback.print_exc()
            clear_torch_cache()
            if raise_generate_gpu_exceptions:
                raise
            return
        else:
            clear_torch_cache()
            if raise_generate_gpu_exceptions:
                raise


def get_generate_params(model_lower, chat,
                        stream_output, show_examples,
                        prompt_type, prompt_dict,
                        temperature, top_p, top_k, num_beams,
                        max_new_tokens, min_new_tokens, early_stopping, max_time,
                        repetition_penalty, num_return_sequences,
                        do_sample,
                        top_k_docs, chunk, chunk_size,
                        verbose):
    use_defaults = False
    use_default_examples = True
    examples = []
    task_info = 'LLM'
    if model_lower:
        print(f"Using Model {model_lower}", flush=True)
    else:
        print("No model defined yet", flush=True)

    min_new_tokens = min_new_tokens if min_new_tokens is not None else 0
    early_stopping = early_stopping if early_stopping is not None else False
    max_time_defaults = 60 * 3
    max_time = max_time if max_time is not None else max_time_defaults

    if not prompt_type and model_lower in inv_prompt_type_to_model_lower:
        prompt_type = inv_prompt_type_to_model_lower[model_lower]
        if verbose:
            print("Auto-selecting prompt_type=%s for %s" % (prompt_type, model_lower), flush=True)

    # examples at first don't include chat, instruction_nochat, iinput_nochat, added at end
    if show_examples is None:
        if chat:
            show_examples = False
        else:
            show_examples = True

    summarize_example1 = """Jeff: Can I train a ? Transformers model on Amazon SageMaker? 
Philipp: Sure you can use the new Hugging Face Deep Learning Container. 
Jeff: ok.
Jeff: and how can I get started? 
Jeff: where can I find documentation? 
Philipp: ok, ok you can find everything here. https://huggingface.co/blog/the-partnership-amazon-sagemaker-and-hugging-face"""

    use_placeholder_instruction_as_example = False
    if 'bart-large-cnn-samsum' in model_lower or 'flan-t5-base-samsum' in model_lower:
        placeholder_instruction = summarize_example1
        placeholder_input = ""
        use_defaults = True
        use_default_examples = False
        use_placeholder_instruction_as_example = True
        task_info = "Summarization"
    elif 't5-' in model_lower or 't5' == model_lower or 'flan-' in model_lower:
        placeholder_instruction = "The square root of x is the cube root of y. What is y to the power of 2, if x = 4?"
        placeholder_input = ""
        use_defaults = True
        use_default_examples = True
        task_info = "Multi-Task: Q/A, translation, Chain-of-Thought, Logical Reasoning, Summarization, etc.  Best to use task prefix as trained on, e.g. `translate English to German: ` (space after colon)"
    elif 'mbart-' in model_lower:
        placeholder_instruction = "The girl has long hair."
        placeholder_input = ""
        use_defaults = True
        use_default_examples = False
        use_placeholder_instruction_as_example = True
    elif 'gpt2' in model_lower:
        placeholder_instruction = "The sky is"
        placeholder_input = ""
        prompt_type = prompt_type or 'plain'
        use_default_examples = True  # some will be odd "continuations" but can be ok
        use_placeholder_instruction_as_example = True
        task_info = "Auto-complete phrase, code, etc."
        use_defaults = True
    else:
        if chat:
            placeholder_instruction = "Enter a question or imperative."
        else:
            placeholder_instruction = "Give detailed answer for whether Einstein or Newton is smarter."
        placeholder_input = ""
        if model_lower:
            # default is plain, because might relly upon trust_remote_code to handle prompting
            prompt_type = prompt_type or 'plain'
        else:
            prompt_type = ''
        task_info = "No task"
        if prompt_type == 'instruct':
            task_info = "Answer question or follow imperative as instruction with optionally input."
        elif prompt_type == 'plain':
            task_info = "Auto-complete phrase, code, etc."
        elif prompt_type == 'human_bot':
            if chat:
                task_info = "Chat (Shift-Enter to give question/imperative, input concatenated with instruction)"
            else:
                task_info = "Ask question/imperative (input concatenated with instruction)"

    # revert to plain if still nothing
    prompt_type = prompt_type or 'plain'
    if use_defaults:
        temperature = 1.0 if temperature is None else temperature
        top_p = 1.0 if top_p is None else top_p
        top_k = 40 if top_k is None else top_k
        num_beams = num_beams or 1
        max_new_tokens = max_new_tokens or 128
        repetition_penalty = repetition_penalty or 1.07
        num_return_sequences = min(num_beams, num_return_sequences or 1)
        do_sample = False if do_sample is None else do_sample
    else:
        temperature = 0.1 if temperature is None else temperature
        top_p = 0.75 if top_p is None else top_p
        top_k = 40 if top_k is None else top_k
        num_beams = num_beams or 1
        max_new_tokens = max_new_tokens or 256
        repetition_penalty = repetition_penalty or 1.07
        num_return_sequences = min(num_beams, num_return_sequences or 1)
        do_sample = False if do_sample is None else do_sample
    # doesn't include chat, instruction_nochat, iinput_nochat, added later
    params_list = ["",
                   stream_output,
                   prompt_type, prompt_dict,
                   temperature, top_p, top_k, num_beams,
                   max_new_tokens, min_new_tokens,
                   early_stopping, max_time, repetition_penalty, num_return_sequences, do_sample]

    if use_placeholder_instruction_as_example:
        examples += [[placeholder_instruction, ''] + params_list]

    if use_default_examples:
        examples += [
            ["Translate English to French", "Good morning"] + params_list,
            ["Give detailed answer for whether Einstein or Newton is smarter.", ''] + params_list,
            ["Explain in detailed list, all the best practices for coding in python.", ''] + params_list,
            [
                "Create a markdown table with 3 rows for the primary colors, and 2 columns, with color name and hex codes.",
                ''] + params_list,
            ['Translate to German:  My name is Arthur', ''] + params_list,
            ["Please answer to the following question. Who is going to be the next Ballon d'or?", ''] + params_list,
            ['Can Geoffrey Hinton have a conversation with George Washington? Give the rationale before answering.',
             ''] + params_list,
            ['Please answer the following question. What is the boiling point of Nitrogen?', ''] + params_list,
            ['Answer the following yes/no question. Can you write a whole Haiku in a single tweet?', ''] + params_list,
            ["Simplify the following expression: (False or False and True). Explain your answer.", ''] + params_list,
            [
                "Premise: At my age you will probably have learnt one lesson. Hypothesis:  It's not certain how many lessons you'll learn by your thirties. Does the premise entail the hypothesis?",
                ''] + params_list,
            ['The square root of x is the cube root of y. What is y to the power of 2, if x = 4?', ''] + params_list,
            [
                'Answer the following question by reasoning step by step.  The cafeteria had 23 apples. If they used 20 for lunch, and bought 6 more, how many apple do they have?',
                ''] + params_list,
            ["""def area_of_rectangle(a: float, b: float):
    \"\"\"Return the area of the rectangle.\"\"\"""", ''] + params_list,
            ["""# a function in native python:
def mean(a):
    return sum(a)/len(a)

# the same function using numpy:
import numpy as np
def mean(a):""", ''] + params_list,
            ["""X = np.random.randn(100, 100)
y = np.random.randint(0, 1, 100)

# fit random forest classifier with 20 estimators""", ''] + params_list,
        ]
    # add summary example
    examples += [
        [summarize_example1, 'Summarize' if prompt_type not in ['plain', 'instruct_simple'] else ''] + params_list]

    src_lang = "English"
    tgt_lang = "Russian"

    # move to correct position
    for example in examples:
        example += [chat, '', '', 'Disabled', top_k_docs, chunk, chunk_size, [DocumentChoices.All_Relevant.name]]
        # adjust examples if non-chat mode
        if not chat:
            example[eval_func_param_names.index('instruction_nochat')] = example[
                eval_func_param_names.index('instruction')]
            example[eval_func_param_names.index('instruction')] = ''

            example[eval_func_param_names.index('iinput_nochat')] = example[eval_func_param_names.index('iinput')]
            example[eval_func_param_names.index('iinput')] = ''
        assert len(example) == len(eval_func_param_names), "Wrong example: %s %s" % (
            len(example), len(eval_func_param_names))

    if prompt_type == PromptType.custom.name and not prompt_dict:
        raise ValueError("Unexpected to get non-empty prompt_dict=%s for prompt_type=%s" % (prompt_dict, prompt_type))

    # get prompt_dict from prompt_type, so user can see in UI etc., or for custom do nothing except check format
    prompt_dict, error0 = get_prompt(prompt_type, prompt_dict,
                                     chat=False, context='', reduced=False, return_dict=True)
    if error0:
        raise RuntimeError("Prompt wrong: %s" % error0)

    return placeholder_instruction, placeholder_input, \
        stream_output, show_examples, \
        prompt_type, prompt_dict, \
        temperature, top_p, top_k, num_beams, \
        max_new_tokens, min_new_tokens, early_stopping, max_time, \
        repetition_penalty, num_return_sequences, \
        do_sample, \
        src_lang, tgt_lang, \
        examples, \
        task_info


def languages_covered():
    # https://huggingface.co/facebook/mbart-large-50-many-to-many-mmt#languages-covered
    covered = """Arabic (ar_AR), Czech (cs_CZ), German (de_DE), English (en_XX), Spanish (es_XX), Estonian (et_EE), Finnish (fi_FI), French (fr_XX), Gujarati (gu_IN), Hindi (hi_IN), Italian (it_IT), Japanese (ja_XX), Kazakh (kk_KZ), Korean (ko_KR), Lithuanian (lt_LT), Latvian (lv_LV), Burmese (my_MM), Nepali (ne_NP), Dutch (nl_XX), Romanian (ro_RO), Russian (ru_RU), Sinhala (si_LK), Turkish (tr_TR), Vietnamese (vi_VN), Chinese (zh_CN), Afrikaans (af_ZA), Azerbaijani (az_AZ), Bengali (bn_IN), Persian (fa_IR), Hebrew (he_IL), Croatian (hr_HR), Indonesian (id_ID), Georgian (ka_GE), Khmer (km_KH), Macedonian (mk_MK), Malayalam (ml_IN), Mongolian (mn_MN), Marathi (mr_IN), Polish (pl_PL), Pashto (ps_AF), Portuguese (pt_XX), Swedish (sv_SE), Swahili (sw_KE), Tamil (ta_IN), Telugu (te_IN), Thai (th_TH), Tagalog (tl_XX), Ukrainian (uk_UA), Urdu (ur_PK), Xhosa (xh_ZA), Galician (gl_ES), Slovene (sl_SI)"""
    covered = covered.split(', ')
    covered = {x.split(' ')[0]: x.split(' ')[1].replace(')', '').replace('(', '') for x in covered}
    return covered


def get_context(chat_context, prompt_type):
    if chat_context and prompt_type == 'human_bot':
        context0 = """<bot>: I am an intelligent, helpful, truthful, and fair assistant named h2oGPT, who will give accurate, balanced, and reliable responses.  I will not respond with I don't know or I don't understand.
<human>: I am a human person seeking useful assistance and request all questions be answered completely, and typically expect detailed responses.  Give answers in numbered list format if several distinct but related items are being listed."""
    else:
        context0 = ''
    return context0


def score_qa(smodel, stokenizer, max_length_tokenize, question, answer, cutoff_len):
    question = question[-cutoff_len:]
    answer = answer[-cutoff_len:]

    inputs = stokenizer(question, answer,
                        return_tensors="pt",
                        truncation=True,
                        max_length=max_length_tokenize).to(smodel.device)
    try:
        score = torch.sigmoid(smodel(**inputs).logits[0]).cpu().detach().numpy()[0]
    except torch.cuda.OutOfMemoryError as e:
        print("GPU OOM 3: question: %s answer: %s exception: %s" % (question, answer, str(e)), flush=True)
        del inputs
        traceback.print_exc()
        clear_torch_cache()
        return 'Response Score: GPU OOM'
    except (Exception, RuntimeError) as e:
        if 'Expected all tensors to be on the same device' in str(e) or \
                'expected scalar type Half but found Float' in str(e) or \
                'probability tensor contains either' in str(e) or \
                'cublasLt ran into an error!' in str(e):
            print("GPU Error: question: %s answer: %s exception: %s" % (question, answer, str(e)),
                  flush=True)
            traceback.print_exc()
            clear_torch_cache()
            return 'Response Score: GPU Error'
        else:
            raise
    os.environ['TOKENIZERS_PARALLELISM'] = 'true'
    return score


def check_locals(**kwargs):
    # ensure everything in evaluate is here
    can_skip_because_locally_generated = no_default_param_names + [
        # get_model:
        'reward_type'
    ]
    for k in eval_func_param_names:
        if k in can_skip_because_locally_generated:
            continue
        assert k in kwargs, "Missing %s" % k
    for k in inputs_kwargs_list:
        if k in can_skip_because_locally_generated:
            continue
        assert k in kwargs, "Missing %s" % k

    for k in list(inspect.signature(get_model).parameters):
        if k in can_skip_because_locally_generated:
            continue
        assert k in kwargs, "Missing %s" % k


def get_max_max_new_tokens(model_state, **kwargs):
    if kwargs['max_new_tokens'] and kwargs['user_set_max_new_tokens']:
        max_max_new_tokens = kwargs['max_new_tokens']
    elif kwargs['memory_restriction_level'] == 1:
        max_max_new_tokens = 768
    elif kwargs['memory_restriction_level'] == 2:
        max_max_new_tokens = 512
    elif kwargs['memory_restriction_level'] >= 3:
        max_max_new_tokens = 256
    else:
        if not isinstance(model_state[1], (str, types.NoneType)):
            max_max_new_tokens = model_state[1].model_max_length
        else:
            # FIXME: Need to update after new model loaded, so user can control with slider
            max_max_new_tokens = 2048
    return max_max_new_tokens


if __name__ == "__main__":
    """
    Examples:

    WORLD_SIZE=4 CUDA_VISIBLE_DEVICES="0,1,2,3" torchrun --nproc_per_node=4 --master_port=1234 generate.py --base_model='EleutherAI/gpt-j-6B' --lora_weights=lora-alpaca_6B
    python generate.py --base_model='EleutherAI/gpt-j-6B' --lora_weights='lora-alpaca_6B'
    python generate.py --base_model='EleutherAI/gpt-neox-20b' --lora_weights='lora-alpaca_20B'
    
    # generate without lora weights, no prompt
    python generate.py --base_model='EleutherAI/gpt-neox-20b' --prompt_type='plain'
    python generate.py --base_model='togethercomputer/GPT-NeoXT-Chat-Base-20B' --prompt_type='dai_faq'

    python generate.py --base_model='togethercomputer/GPT-NeoXT-Chat-Base-20B' --prompt_type='dai_faq' --lora_weights='lora_20B_daifaq'
    # OpenChatKit settings:
    python generate.py --base_model='togethercomputer/GPT-NeoXT-Chat-Base-20B' --prompt_type='human_bot --debug=True --num_beams=1 --temperature=0.6 --top_k=40 --top_p=1.0

    python generate.py --base_model='distilgpt2' --prompt_type='plain' --debug=True --num_beams=1 --temperature=0.6 --top_k=40 --top_p=1.0 --share=False
    python generate.py --base_model='t5-large' --prompt_type='simple_instruct'
    python generate.py --base_model='philschmid/bart-large-cnn-samsum'
    python generate.py --base_model='philschmid/flan-t5-base-samsum'
    python generate.py --base_model='facebook/mbart-large-50-many-to-many-mmt'

    python generate.py --base_model='togethercomputer/GPT-NeoXT-Chat-Base-20B' --prompt_type='human_bot' --lora_weights='GPT-NeoXT-Chat-Base-20B.merged.json.8_epochs.57b2892c53df5b8cefac45f84d019cace803ef26.28'

    must have 4*48GB GPU and run without 8bit in order for sharding to work with infer_devices=False
    can also pass --prompt_type='human_bot' and model can somewhat handle instructions without being instruct tuned
    python generate.py --base_model=decapoda-research/llama-65b-hf --load_8bit=False --infer_devices=False --prompt_type='human_bot'

    python generate.py --base_model=h2oai/h2ogpt-oig-oasst1-512-6_9b
    """
    fire.Fire(main)
