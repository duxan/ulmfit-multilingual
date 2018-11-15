"""
Train a classifier on top of a language model trained with `pretrain_lm.py`.
Optionally fine-tune LM before.
"""
import numpy as np
import pickle

import torch
from fastai.text import TextLMDataBunch, TextClasDataBunch, language_model_learner, text_classifier_learner
from fastai import fit_one_cycle
from fastai_contrib.utils import PAD, UNK, read_clas_data, PAD_TOKEN_ID, DATASETS, TRN, VAL, TST
from fastai.text.transform import Vocab

import fire
from collections import Counter
from pathlib import Path


def new_train_clas(data_dir, lang='en', cuda_id=0, pretrain_name='wt-103', model_dir='models', qrnn=True,
                   fine_tune=True, max_vocab=30000, bs=70, bptt=70, name='imdb-clas',
                   dataset='imdb'):
    """
    :param data_dir: The path to the `data` directory
    :param lang: the language unicode
    :param cuda_id: The id of the GPU. Uses GPU 0 by default or no GPU when
                    run on CPU.
    :param pretrain_name: name of the pretrained model
    :param model_dir: The path to the directory where the pretrained model is saved
    :param qrrn: Use a QRNN. Requires installing cupy.
    :param fine_tune: Fine-tune the pretrained language model
    :param max_vocab: The maximum size of the vocabulary.
    :param bs: The batch size.
    :param bptt: The back-propagation-through-time sequence length.
    :param name: The name used for both the model and the vocabulary.
    :param dataset: The dataset used for evaluation. Currently only IMDb and
                    XNLI are implemented. Assumes dataset is located in `data`
                    folder and that name of folder is the same as dataset name.
    """
    if not torch.cuda.is_available():
        print('CUDA not available. Setting device=-1.')
        cuda_id = -1
    torch.cuda.set_device(cuda_id)

    print(f'Dataset: {dataset}. Language: {lang}.')
    assert dataset in DATASETS, f'Error: {dataset} processing is not implemented.'
    assert (dataset == 'imdb' and lang == 'en') or not dataset == 'imdb',\
        'Error: IMDb is only available in English.'

    data_dir = Path(data_dir)
    assert data_dir.name == 'data',\
        f'Error: Name of data directory should be data, not {data_dir.name}.'
    dataset_dir = data_dir / dataset
    model_dir = Path(model_dir)
    assert data_dir.exists(), f'Error: {data_dir} does not exist.'
    assert dataset_dir.exists(), f'Error: {dataset_dir} does not exist.'
    assert model_dir.exists(), f'Error: {model_dir} does not exist.'

    if qrnn:
        print('Using QRNNs...')
    model_name = 'qrnn' if qrnn else 'lstm'

    tmp_dir = dataset_dir / 'tmp'
    tmp_dir.mkdir(exist_ok=True)
    vocab_file = tmp_dir / f'vocab_{lang}.pkl'

    if not (tmp_dir / f'{TRN}_{lang}_ids.npy').exists():
        print('Reading the data...')
        toks, lbls = read_clas_data(dataset_dir, dataset, lang)

        # create the vocabulary
        counter = Counter(word for example in toks[TRN] for word in example)
        itos = [word for word, count in counter.most_common(n=max_vocab)]
        itos.insert(0, PAD)
        itos.insert(0, UNK)
        vocab = Vocab(itos)
        stoi = vocab.stoi
        with open(vocab_file, 'wb') as f:
            pickle.dump(vocab, f)

        ids = {}
        for split in [TRN, VAL, TST]:
            ids[split] = np.array([([stoi.get(w, stoi[UNK]) for w in s])
                                   for s in toks[split]])
            np.save(tmp_dir / f'{split}_{lang}_ids.npy', ids[split])
            np.save(tmp_dir / f'{split}_{lang}_lbl.npy', lbls[split])
    else:
        print('Loading the pickled data...')
        ids, lbls = {}, {}
        for split in [TRN, VAL, TST]:
            ids[split] = np.load(tmp_dir / f'{split}_{lang}_ids.npy')
            lbls[split] = np.load(tmp_dir / f'{split}_{lang}_lbl.npy')
        with open(vocab_file, 'rb') as f:
            vocab = pickle.load(f)

    print(f'Train size: {len(ids[TRN])}. Valid size: {len(ids[VAL])}. '
          f'Test size: {len(ids[TST])}.')

    data_lm = TextLMDataBunch.from_ids(path=tmp_dir, vocab=vocab, trn_ids=ids[TRN],
                                       val_ids=ids[VAL], bs=bs, bptt=bptt)

    # TODO TextClasDataBunch allows tst_ids as input, but not tst_lbls?
    data_clas = TextClasDataBunch.from_ids(
        path=tmp_dir, vocab=vocab, trn_ids=ids[TRN], val_ids=ids[VAL],
        trn_lbls=lbls[TRN], val_lbls=lbls[VAL], bs=bs)

    if qrnn:
        emb_sz, nh, nl = 400, 1550, 3
    else:
        emb_sz, nh, nl = 400, 1150, 3
    learn = language_model_learner(
        data_lm, bptt=bptt, emb_sz=emb_sz, nh=nh, nl=nl, qrnn=qrnn,
        pad_token=PAD_TOKEN_ID,
        pretrained_fnames=(f'lstm_{pretrain_name}', f'itos_{pretrain_name}'),
        path=model_dir.parent, model_dir=model_dir.name)

    if fine_tune:
        print('Fine-tuning the language model...')
        learn.unfreeze()
        learn.fit(2, slice(1e-4, 1e-2))

        # save encoder
        learn.save_encoder('enc')

    learn = text_classifier_learner(data_clas, bptt=bptt, pad_token=PAD_TOKEN_ID,
                                  path=model_dir.parent, model_dir=model_dir.name,
                                  qrnn=qrnn, emb_sz=emb_sz, nh=nh, nl=nl)

    learn.load_encoder('enc')
    fit_one_cycle(learn, 1, 5e-3, (0.8, 0.7), wd=1e-7)

    learn.freeze_to(-2)
    fit_one_cycle(learn, 1, 5e-3, (0.8, 0.7), wd=1e-7)

    learn.unfreeze()
    fit_one_cycle(learn, 10, 5e-3, (0.8, 0.7), wd=1e-7)

    print(f"Saving models at {learn.path / learn.model_dir}")
    learn.save(f'{model_name}_{name}')


if __name__ == '__main__':
    fire.Fire(new_train_clas)
