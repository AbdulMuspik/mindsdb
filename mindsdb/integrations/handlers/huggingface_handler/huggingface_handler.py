from typing import Optional

import pandas as pd
import transformers
from huggingface_hub import HfApi

from mindsdb.utilities import log
from mindsdb.utilities.device import get_devices

from mindsdb.integrations.libs.base import BaseMLEngine


class HuggingFaceHandler(BaseMLEngine):
    name = 'huggingface'

    @staticmethod
    def create_validation(target, args=None, **kwargs):

        if 'using' in args:
            args = args['using']

        hf_api = HfApi()

        # check model is pytorch based
        metadata = hf_api.model_info(args['model_name'])
        if 'pytorch' not in metadata.tags:
            raise Exception('Currently only PyTorch models are supported (https://huggingface.co/models?library=pytorch&sort=downloads). To request another library, please contact us on our community slack (https://mindsdb.com/joincommunity).')

        # check model task
        supported_tasks = ['text-classification',
                           'zero-shot-classification',
                           'translation',
                           'summarization',
                           'text2text-generation',
                           'fill-mask']

        if metadata.pipeline_tag not in supported_tasks:
            raise Exception(f'Not supported task for model: {metadata.pipeline_tag}.\
             Should be one of {", ".join(supported_tasks)}')

        if 'task' not in args:
            args['task'] = metadata.pipeline_tag
        elif args['task'] != metadata.pipeline_tag:
            raise Exception(f'Task mismatch for model: {args["task"]}!={metadata.pipeline_tag}')

        input_keys = list(args.keys())

        # task, model_name, input_column is essential
        for key in ['task', 'model_name', 'input_column']:
            if key not in args:
                raise Exception(f'Parameter "{key}" is required')
            input_keys.remove(key)

        # check tasks input

        if args['task'] == 'zero-shot-classification':
            key = 'candidate_labels'
            if key not in args:
                raise Exception('"candidate_labels" is required for zero-shot-classification')
            input_keys.remove(key)

        if args['task'] == 'translation':
            keys = ['lang_input', 'lang_output']
            for key in keys:
                if key not in args:
                    raise Exception(f'{key} is required for translation')
                input_keys.remove(key)

        if args['task'] == 'summarization':
            keys = ['min_output_length', 'max_output_length']
            for key in keys:
                if key not in args:
                    raise Exception(f'{key} is required for translation')
                input_keys.remove(key)

        # optional keys
        for key in ['labels', 'max_length', 'truncation_policy']:
            if key in input_keys:
                input_keys.remove(key)

        if len(input_keys) > 0:
            raise Exception(f'Not expected parameters: {", ".join(input_keys)}')

    def create(self, target, args=None, **kwargs):
        # TODO change BaseMLEngine api?
        if 'using' in args:
            args = args['using']

        args['target'] = target

        model_name = args['model_name']
        hf_model_storage_path = self.engine_storage.folder_get(model_name)  # real

        if args['task'] == 'translation':
            args['task_proper'] = f"translation_{args['lang_input']}_to_{args['lang_output']}"
        else:
            args['task_proper'] = args['task']

        log.logger.debug(f"Checking file system for {model_name}...")

        ####
        # Check if pipeline has already been downloaded
        # TODO: add GPU support here, too
        try:
            pipeline = transformers.pipeline(task=args['task_proper'], model=hf_model_storage_path,
                                             tokenizer=hf_model_storage_path)
            log.logger.debug(f'Model already downloaded!')
        ####
        # Otherwise download it
        except OSError:
            try:
                log.logger.debug(f"Downloading {model_name}...")
                pipeline = transformers.pipeline(task=args['task_proper'], model=model_name)

                pipeline.save_pretrained(hf_model_storage_path)

                log.logger.debug(f"Saved to {hf_model_storage_path}")
            except Exception:
                raise Exception("Error while downloading and setting up the model. Please try a different model. We're working on expanding the list of supported models, so we would appreciate it if you let us know about this in our community slack (https://mindsdb.com/joincommunity).")  # noqa
        ####

        if 'max_length' in args:
            pass
        elif 'max_position_embeddings' in pipeline.model.config.to_dict().keys():
            args['max_length'] = pipeline.model.config.max_position_embeddings
        elif 'max_length' in pipeline.model.config.to_dict().keys():
            args['max_length'] = pipeline.model.config.max_length
        else:
            log.logger.debug('No max_length found!')

        labels_default = pipeline.model.config.id2label
        labels_map = {}
        if 'labels' in args:
            for num in labels_default.keys():
                labels_map[labels_default[num]] = args['labels'][num]
            args['labels_map'] = labels_map
        else:
            for num in labels_default.keys():
                labels_map[labels_default[num]] = labels_default[num]
            args['labels_map'] = labels_map

        ###### store and persist in model folder
        self.model_storage.json_set('args', args)

        ###### persist changes to handler folder
        self.engine_storage.folder_sync(model_name)

    def predict_text_classification(self, pipeline, item, args):
        top_k = args.get('top_k', 1000)

        result = pipeline([item], top_k=top_k, truncation=True, max_length=args['max_length'])[0]

        final = {}
        explain = {}
        if type(result) == dict:
            result = [result]
        final[args['target']] = args['labels_map'][result[0]['label']]
        for elem in result:
            if args['labels_map']:
                explain[args['labels_map'][elem['label']]] = elem['score']
            else:
                explain[elem['label']] = elem['score']
        final[f"{args['target']}_explain"] = explain
        return final

    def predict_zero_shot(self, pipeline, item, args):
        top_k = args.get('top_k', 1000)

        result = pipeline([item], candidate_labels=args['candidate_labels'],
                                     truncation=True, top_k=top_k, max_length=args['max_length'])[0]

        final = {}
        final[args['target']] = result['labels'][0]

        explain = dict(zip(result['labels'], result['scores']))
        final[f"{args['target']}_explain"] = explain

        return final

    def predict_translation(self, pipeline, item, args):
        result = pipeline([item], max_length=args['max_length'])[0]

        final = {}
        final[args['target']] = result['translation_text']

        return final

    def predict_summarization(self, pipeline, item, args):
        result = pipeline([item], min_length=args['min_output_length'], max_length=args['max_output_length'])[0]

        final = {}
        final[args['target']] = result['summary_text']

        return final

    def predict_text2text(self, pipeline, item, args):
        result = pipeline([item], max_length=args['max_length'])[0]

        final = {}
        final[args['target']] = result['generated_text']

        return final

    def predict_fill_mask(self, pipeline, item, args):
        result = pipeline([item])[0]

        final = {}
        final[args['target']] = result[0]['sequence']
        explain = {elem['sequence']: elem['score'] for elem in result}
        final[f"{args['target']}_explain"] = explain

        return final

    def predict(self, df, args=None):

        fnc_list = {
            'text-classification': self.predict_text_classification,
            'zero-shot-classification': self.predict_zero_shot,
            'translation': self.predict_translation,
            'summarization': self.predict_summarization,
            'fill-mask': self.predict_fill_mask
        }

        ###### get stuff from model folder
        args = {**self.model_storage.json_get('args'), **args.get('predict_params', {})}

        task = args['task']

        if task not in fnc_list:
            raise RuntimeError(f'Unknown task: {task}')

        hf_model_storage_path = self.engine_storage.folder_get(args['model_name'], update=False)

        _, device_id = get_devices()  # If device_id == 0: cpu. Else: # of available GPUs.
        device = device_id - 1

        pipeline = transformers.pipeline(task=args['task_proper'], model=hf_model_storage_path,
                                         tokenizer=hf_model_storage_path, device=device)

        input_column = args['input_column']
        if input_column not in df.columns:
            raise RuntimeError(f'Column "{input_column}" not found in input data')
        input_list = df[input_column]

        batch_size = args.get('batch_size', 1)
        tokenizer_kwargs = {
            'padding': True,
            'truncation': True
        }
        if batch_size > 1:
            data = input_list.tolist()
            try:
                results = pipeline(data, *args, batch_size=batch_size, **tokenizer_kwargs)
            except Exception as e:
                msg = str(e).strip()
                if msg == '':
                    msg = e.__class__.__name__
                results = [{'error': msg}] * input_list.shape[0]
        else:
            results = []
            for item in input_list:
                try:
                    result = fnc(pipeline, item, *args, **tokenizer_kwargs)
                except Exception as e:
                    msg = str(e).strip()
                    if msg == '':
                        msg = e.__class__.__name__
                    result = {'error': msg}
                results.append(result[0])

        pred_df = pd.DataFrame(results)

        return pred_df

    def describe(self, attribute: Optional[str] = None) -> pd.DataFrame:

        args = self.model_storage.json_get('args')

        if attribute == 'args':
            return pd.DataFrame(args.items(), columns=['key', 'value'])
        elif attribute == 'metadata':
            hf_api = HfApi()
            metadata = hf_api.model_info(args['model_name'])
            data = metadata.__dict__
            return pd.DataFrame(list(data.items()), columns=['key', 'value'])
        else:
            tables = ['args', 'metadata']
            return pd.DataFrame(tables, columns=['tables'])
