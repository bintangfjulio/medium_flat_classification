import torch
import os
import re
import pytorch_lightning as pl
import multiprocessing
import string
import pickle

from Sastrawi.StopWordRemover.StopWordRemoverFactory import StopWordRemoverFactory
from Sastrawi.Stemmer.StemmerFactory import StemmerFactory
from transformers import BertTokenizer
from torch.utils.data import TensorDataset, DataLoader

class Preprocessor(pl.LightningDataModule):
    def __init__(self, batch_size, dataset, num_classes, hierarchy_tree):
        super(Preprocessor, self).__init__()
        self.batch_size = batch_size
        self.dataset = dataset
        self.num_classes = num_classes
        self.hierarchy_tree = hierarchy_tree
        self.stop_words = StopWordRemoverFactory().get_stop_words()
        self.stemmer = StemmerFactory().create_stemmer()
        self.tokenizer = BertTokenizer.from_pretrained('indolem/indobert-base-uncased')

    def setup(self, stage=None):
        train_set, valid_set, test_set = self.preprocessor()   
        if stage == "fit":
            self.train_set = train_set
            self.valid_set = valid_set
        elif stage == "test":
            self.test_set = test_set

    def train_dataloader(self):
        return DataLoader(
            dataset=self.train_set,
            batch_size=self.batch_size,
            shuffle=True,
            num_workers=multiprocessing.cpu_count()
        )

    def val_dataloader(self):
        return DataLoader(
            dataset=self.valid_set,
            batch_size=self.batch_size,
            num_workers=multiprocessing.cpu_count()
        )

    def test_dataloader(self):
        return DataLoader(
            dataset=self.test_set,
            batch_size=self.batch_size,
            num_workers=multiprocessing.cpu_count()
        )
    
    def preprocessor(self):
        if os.path.exists("datasets/train_set.pt") and os.path.exists("datasets/valid_set.pt") and os.path.exists("datasets/test_set.pt"):
            print("\nLoading Data...")
            train_set = torch.load("datasets/train_set.pt")
            valid_set = torch.load("datasets/valid_set.pt")
            test_set = torch.load("datasets/test_set.pt")
            print('[ Loading Completed ]\n')
        else:
            print("\nPreprocessing Data...")
            train_set, valid_set, test_set = self.preprocessing_data(self.dataset)
            print('[ Preprocessing Completed ]\n')

        return train_set, valid_set, test_set

    def get_max_length(self, dataset, extra_length=10):
        sentences_token = []
        
        for data in dataset.values.tolist():
            data = str(data[0]).split()
            sentences_token.append(data)

        token_length = [len(token) for token in sentences_token]
        max_length = max(token_length) + extra_length
        
        return max_length

    def generate_hierarchy_segment(self):
        segmented_parent_child = {}

        with open(self.hierarchy_tree, "r") as tree:
            for path in tree:
                nodes = path[:-1].lower().split(" > ")

                for depth, node in enumerate(nodes):
                    if depth > 0:
                        parent = nodes[depth - 1]
                        try:
                            segmented_parent_child[parent].add(node)
                        except:
                            segmented_parent_child[parent] = set()
                            segmented_parent_child[parent].add(node)
    
        return segmented_parent_child

    def preprocessing_data(self, dataset): 
        dataset = dataset.dropna()
        
        labels_idx = dataset['leaf'].unique().tolist()
        dataset['label_idx'] = dataset['leaf'].map(lambda x: labels_idx.index(x))

        segmented_parent_child = self.generate_hierarchy_segment()
        parents_idx = {parent: index for index, parent in enumerate(segmented_parent_child.keys())}        
        segmented_by_hierarchy = [[] for i in range(len(parents_idx))]

        flat_input_ids, flat_target = [], []
        max_length = self.get_max_length(dataset)

        for data in dataset.values.tolist():
            name = self.data_cleaning(str(data[0])) 
            flat_binary = [0] * self.num_classes
            flat_binary[int(data[4])] = 1

            token = self.tokenizer(text=name, max_length=max_length, padding="max_length", truncation=True)  

            path = data[3]
            nodes = path.lower().split(" > ")

            for depth, node in enumerate(nodes[:-1]):
                child = nodes[depth + 1]
                child_of_parent = list(segmented_parent_child[node])
                child_idx = child_of_parent.index(child)

                hierarchical_binary = [0] * len(child_of_parent)
                hierarchical_binary[child_idx] = 1
                
                parent_idx = parents_idx[node]

                if 'input_ids' not in segmented_by_hierarchy[parent_idx]:
                    segmented_by_hierarchy[parent_idx] = {'input_ids': [], 'hierarchical_target': []}
                
                segmented_by_hierarchy[parent_idx]['input_ids'].append(token['input_ids'])
                segmented_by_hierarchy[parent_idx]['hierarchical_target'].append(hierarchical_binary)
            
            flat_input_ids.append(token['input_ids'])
            flat_target.append(flat_binary)

        hierarchical_dataset = []

        for data in segmented_by_hierarchy:
            hierarchical_input_ids = data['input_ids']
            hierarchical_target = data['hierarchical_target']
            
            train_set, valid_set, test_set = self.data_splitting(hierarchical_input_ids, hierarchical_target)
            hierarchical_dataset.append([train_set, valid_set, test_set])

        with open("datasets/hierarchical_dataset.pkl", "wb") as hierarchical_preprocessed:
            pickle.dump(hierarchical_dataset, hierarchical_preprocessed, protocol=pickle.HIGHEST_PROTOCOL)

        train_set, valid_set, test_set = self.data_splitting(flat_input_ids, flat_target)
        torch.save(train_set, "datasets/train_set.pt")
        torch.save(valid_set, "datasets/valid_set.pt")
        torch.save(test_set, "datasets/test_set.pt")

        return train_set, valid_set, test_set
    
    def data_cleaning(self, text):
        text = text.lower()
        text = re.sub(r"[^A-Za-z0-9(),!?\'\-`]", " ", text)
        text = re.sub('\n', ' ', text)
        text = re.sub(r'@\w+', '', text)
        text = re.sub(r'http\S+', '', text)
        text = text.translate(str.maketrans('', '', string.punctuation))
        text = re.sub("'", '', text)
        text = re.sub(r'\d+', '', text)
        text = ' '.join([word for word in text.split() if word not in self.stop_words and len(word) > 1])
        text = self.stemmer.stem(text.strip())

        return text
    
    def data_splitting(self, input_ids, target):
        input_ids = torch.tensor(input_ids)
        target = torch.tensor(target)
        
        tensor_dataset = TensorDataset(input_ids, target)

        train_valid_size = round(len(tensor_dataset) * 0.8)
        test_size = len(tensor_dataset) - train_valid_size

        train_valid_set, test_set = torch.utils.data.random_split(tensor_dataset, [train_valid_size, test_size])

        train_size = round(len(train_valid_set) * 0.9)
        valid_size = len(train_valid_set) - train_size

        train_set, valid_set = torch.utils.data.random_split(train_valid_set, [train_size, valid_size])

        return train_set, valid_set, test_set
