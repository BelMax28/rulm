import os
from collections import defaultdict
from typing import List, Tuple, Type, Generator, Any
import gzip

import pygtrie
import numpy as np
from allennlp.data.vocabulary import Vocabulary
from allennlp.common.util import START_SYMBOL, END_SYMBOL

from rulm.language_model import  LanguageModel
from rulm.transform import Transform


class NGramContainer:
    def __getitem__(self, n_gram: List[int]):
        raise NotImplementedError()

    def __setitem__(self, n_gram: List[int], value: float):
        raise NotImplementedError()

    def __len__(self):
        raise NotImplementedError()

    def __repr__(self):
        raise NotImplementedError()

    def items(self):
        raise NotImplementedError()


class DictNGramContainer(NGramContainer):
    def __init__(self):
        self.data = defaultdict(float)

    def __getitem__(self, n_gram: List[int]):
        return self.data[tuple(n_gram)]

    def __setitem__(self, n_gram: List[int], value: float):
        self.data[tuple(n_gram)] = value

    def __len__(self):
        return len(self.data)

    def __repr__(self):
        return repr(self.data)

    def items(self):
        return self.data.items()


class TrieNGramContainer(NGramContainer):
    def __init__(self):
        self.data = pygtrie.Trie()
        self.n = None

    def __getitem__(self, n_gram: List[int]):
        return self.data[tuple(n_gram)] if n_gram in self.data else 0.

    def __setitem__(self, n_gram: List[int], value: float):
        self.data[tuple(n_gram)] = value

    def __len__(self):
        return len(self.data)

    def __repr__(self):
        return repr(self.data)

    def items(self):
        return self.data.items()


class NGramLanguageModel(LanguageModel):
    def __init__(self, n: int, vocabulary: Vocabulary,
                 transforms: Tuple[Transform]=tuple(),
                 reverse: bool=False,
                 interpolation_lambdas: Tuple[float, ...]=None,
                 container: Type[NGramContainer]=DictNGramContainer):
        assert not interpolation_lambdas or n == len(interpolation_lambdas)
        self.n_grams = [container() for _ in range(n+1)]  # type: List[NGramContainer]
        self.n = n  # type: int
        self.interpolation_lambdas = interpolation_lambdas  # type: Tuple[float]
        LanguageModel.__init__(self, vocabulary, transforms, reverse)

    def _collect_n_grams(self, indices: List[int]) -> None:
        l = len(indices)
        for n in range(self.n + 1):
            for i in range(min(l - n + 1, l)):
                n_gram = indices[i:i+n]
                self.n_grams[n][n_gram] += 1.0

    def train(self, inputs: Generator[List[str], Any, None], report_every: int=10000):
        sentence_number = 0
        for sentence in inputs:
            indices = self._numericalize_inputs(sentence)
            eos_index = self.vocabulary.get_token_index(END_SYMBOL)
            indices.append(eos_index)
            self._collect_n_grams(indices)
            sentence_number += 1
            if sentence_number % report_every == 0:
                print("Train: {} sentences processed".format(sentence_number))

    def train_file(self, file_name):
        assert os.path.exists(file_name)
        sentences = self._parse_file_for_train(file_name)
        self.train(sentences)
        print("Train: normalizng...")
        self.normalize()

    def normalize(self):
        for n in range(self.n, 0, -1):
            current_n_grams = self.n_grams[n]
            for words, count in current_n_grams.items():
                prev_order_n_gram_count = self.n_grams[n-1][words[:-1]]
                current_n_grams[words] = count / prev_order_n_gram_count
        self.n_grams[0][tuple()] = 1.0

    def predict(self, indices: List[int]) -> np.ndarray:
        probabilities = np.zeros(self.vocabulary.get_vocab_size(), dtype=np.float64)
        if not self.interpolation_lambdas:
            self.interpolation_lambdas = (1.0, ) + tuple((0. for _ in range(self.n-1)))
        context = tuple(indices[-self.n+1:])
        for shift in range(self.n):
            wanted_context_length = self.n-1-shift
            if wanted_context_length > len(context) or self.interpolation_lambdas[shift] == 0.:
                continue
            difference = len(context) - wanted_context_length
            context = tuple(context[difference:])
            n = len(context) + 1
            for index in range(probabilities.shape[0]):
                n_gram = context + (index,)
                p = self.n_grams[n][n_gram]
                probabilities[index] += p * self.interpolation_lambdas[shift]
        return probabilities

    def save(self, path: str) -> None:
        assert path.endswith(".arpa") or path.endswith(".arpa.gzip")
        file_open = gzip.open if path.endswith(".gzip") else open
        with file_open(path, "wt", encoding="utf-8") as w:
            w.write("\\data\\\n")
            for n in range(1, self.n+1):
                w.write("ngram {}={}\n".format(n, len(self.n_grams[n])))
            w.write("\n")
            for n in range(1, self.n+1):
                w.write("\\{}-grams:\n".format(n))
                for words, p in self.n_grams[n].items():
                    words = " ".join(map(self.vocabulary.get_token_from_index, words))
                    w.write("{:.4f}\t{}\n".format(np.log10(p), words))
                w.write("\n")
            w.write("\\end\\\n")

    def load(self, path: str) -> None:
        self.n_grams[0][tuple()] = 1.
        file_open = gzip.open if path.endswith(".gzip") else open
        with file_open(path, "rt", encoding="utf-8") as r:
            line = next(r)
            while not line.strip():
                line = next(r)
            assert line.strip() == "\\data\\", "Invalid ARPA: missing \\data\\"
            max_n = 0
            for line in r:
                if not line.startswith("ngram"):
                    break
                n = int(line.strip().split()[1].split("=")[0])
                max_n = max(max_n, n)
            assert max_n == self.n, "Invalid ARPA: wrong max n"
            for n in range(1, self.n+1):
                while not line.strip():
                    line = next(r)
                assert line.strip() == "\\{}-grams:".format(n), "Invalid ARPA: wrong {}-gram start".format(n)
                for line in r:
                    if not line.strip():
                        break
                    tokens = line.strip().split()
                    p = float(tokens[0])
                    words = tuple(map(self.vocabulary.get_token_index, tokens[1:n+1]))
                    self.n_grams[n][words] = np.power(10, p)
            while not line.strip():
                line = next(r)
            assert line.strip() == "\\end\\", "Invalid ARPA: \\end\\ invalid or missing"
