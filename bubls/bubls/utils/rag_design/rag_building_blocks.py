from bubls.utils.data.download import download_file_from_url
from llama_index.core import (
    SimpleDirectoryReader,
    VectorStoreIndex,
    StorageContext,
    load_index_from_storage,
    Settings,
)
from llama_index.core.evaluation import (
    DatasetGenerator,
    EmbeddingQAFinetuneDataset,
)
from llama_index.llms.openai import OpenAI
from llama_index.embeddings.openai import OpenAIEmbedding
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.ingestion import IngestionPipeline
from llama_index.core.memory import ChatMemoryBuffer
from llama_index.core.tools import QueryEngineTool, ToolMetadata
from llama_index.finetuning import generate_qa_embedding_pairs
from llama_index.readers.wikipedia import WikipediaReader
from typing import Any, Dict
import pandas as pd
import pickle
import random
import os
import nest_asyncio
from tqdm.notebook import tqdm


nest_asyncio.apply()
Settings.llm = OpenAI(temperature=0.2, model="gpt-3.5-turbo")
Settings.embed_model = OpenAIEmbedding(name="text-embedding-ada-002")


class RAGBuildingBlocks:
    def __init__(self, components_cfg: dict):
        self.components_cfg = components_cfg
        self.documents = {}
        self.llm_eval_data = {}
        self.nodes = {}
        self.qa_pairs = {}
        self.index = {}
        self.query_engine = {}
        self.retriever = {}
        self.chat_engine = {}
        self.query_engine_tools = []
        self.eval_data = {}

    def _ingest_data(self, c_id: str):
        component_cfg = self.components_cfg[c_id]

        # Nodes is the main output, check if exists
        nodes_data_path = os.path.join(
            os.environ["PERSIST_DIR"], c_id, "nodes"
        )
        if not os.path.exists(nodes_data_path):
            print(f"Generating data artifacts for {c_id}")
            self._download_data(c_id, component_cfg.get("download_data"))
            self._load_data(c_id, component_cfg.get("load_data"))
            self._gen_llm_eval_data(
                c_id, component_cfg.get("gen_llm_eval_data", {})
            )
            self._gen_nodes(c_id, component_cfg.get("gen_nodes", {}))
            self._gen_qa_pairs(c_id, component_cfg.get("gen_qa_pairs", {}))
        else:
            print(f"Loading data artifacts for {c_id}")
            self._get_llm_eval_data(c_id)
            self._get_nodes(c_id)
            self.get_qa_pairs(c_id)

    def _download_data(self, c_id: str, cfg: Dict[str, Any]):
        if not cfg:
            return
        for source_url, file_name in zip(
            cfg["source_urls"], cfg["file_names"]
        ):
            save_data_to = os.path.join(os.environ["DATA_DIR"], c_id)
            download_file_from_url(
                source_url,
                file_name,
                save_data_to,
            )
            print(f"File {file_name} available at {save_data_to}")

    def _load_data(self, c_id: str, cfg: Dict[str, Any]):
        if cfg["source"] == "local":
            self._load_data_local(c_id, cfg)
        elif cfg["source"] == "wikipedia":
            self._load_data_wikipedia(c_id, cfg)
        else:
            raise ValueError(f"{cfg['source']} not supported.")

    def _load_data_local(self, c_id: str, cfg: Dict[str, Any]):
        print(f"Loading local data {c_id}")
        self.documents[c_id] = SimpleDirectoryReader(
            os.path.join(os.environ["DATA_DIR"], c_id),
            file_extractor=cfg.get("file_extractor", {}),
        ).load_data()

    def _load_data_wikipedia(self, c_id: str, cfg: Dict[str, Any]):
        print(f"Loading wikipedia data {c_id}")
        reader = WikipediaReader()
        self.documents[c_id] = reader.load_data(pages=cfg["pages"])

    def _gen_llm_eval_data(self, c_id: str, cfg: Dict[str, Any]):
        persist_dir = os.path.join(
            os.environ["PERSIST_DIR"], c_id, "llm_eval_data"
        )
        os.makedirs(persist_dir, exist_ok=True)
        self.llm_eval_data[c_id] = {}
        docs_pct_split = cfg.get("docs_pct_split", [0.5, 0.3, 0.2])
        if sum(docs_pct_split) > 1:
            raise ValueError(
                f" Sum of docs_pct_split elements can't be higher than 1"
            )
        docs_n_split = [
            int(pct * len(self.documents[c_id])) for pct in docs_pct_split
        ]
        random_docs = random.sample(
            self.documents[c_id], len(self.documents[c_id])
        )
        i = 0
        for s, split in enumerate(["train", "val", "test"]):
            print(f"Generating LLM Eval Data for {c_id}, {split}")
            split_docs = random_docs[i : i + docs_n_split[s]]
            i += docs_n_split[s]
            data_generator = DatasetGenerator.from_documents(
                split_docs,
                num_questions_per_chunk=cfg.get("num_questions_per_chunk", 2),
            )

            self.llm_eval_data[c_id][
                split
            ] = data_generator.generate_questions_from_nodes()

            data_path = os.path.join(persist_dir, f"llm_eval_data_{split}.pkl")
            with open(data_path, "wb") as file:
                pickle.dump(self.llm_eval_data[c_id][split], file)

    def _get_llm_eval_data(self, c_id: str):
        self.llm_eval_data[c_id] = {}
        persist_dir = os.path.join(
            os.environ["PERSIST_DIR"], c_id, "llm_eval_data"
        )
        for split in ["train", "val", "test"]:
            data_path = os.path.join(persist_dir, f"llm_eval_data_{split}.pkl")
            print(f"Loading LLM eval data for {c_id}, {split}")
            with open(data_path, "rb") as file:
                self.llm_eval_data[c_id][split] = pickle.load(file)

    def _gen_nodes(self, c_id: str, cfg: Dict[str, Any]):
        persist_dir = os.path.join(os.environ["PERSIST_DIR"], c_id, "nodes")
        os.makedirs(persist_dir, exist_ok=True)
        self.nodes[c_id] = {}

        ## Using node_parser
        # node_parser = SentenceSplitter(**cfg)
        # self.nodes[c] = node_parser.get_nodes_from_documents(
        #     self.documents[c], show_progress=False
        # )

        ## Using transformation pipeline
        transformations = cfg.get("transformations", [SentenceSplitter()])
        pipeline = IngestionPipeline(transformations=transformations)

        docs_pct_split = cfg.get("docs_pct_split", [0.5, 0.3, 0.2])
        if sum(docs_pct_split) > 1:
            raise ValueError(
                f" Sum of docs_pct_split elements can't be higher than 1"
            )
        docs_n_split = [
            int(pct * len(self.documents[c_id])) for pct in docs_pct_split
        ]
        random_docs = random.sample(
            self.documents[c_id], len(self.documents[c_id])
        )
        i = 0
        for s, split in enumerate(["train", "val", "test"]):
            print(f"Generating Nodes for {c_id}, {split}")
            split_docs = random_docs[i : i + docs_n_split[s]]
            self.nodes[c_id][split] = pipeline.run(documents=split_docs)
            i += docs_n_split[s]
            data_path = os.path.join(persist_dir, f"nodes_{split}.pkl")
            with open(data_path, "wb") as file:
                pickle.dump(self.nodes[c_id][split], file)

    def _get_nodes(self, c_id: str):
        self.nodes[c_id] = {}
        persist_dir = os.path.join(os.environ["PERSIST_DIR"], c_id, "nodes")
        for split in ["train", "val", "test"]:
            print(f"Generating Nodes for {c_id}, {split}")
            data_path = os.path.join(persist_dir, f"nodes_{split}.pkl")
            with open(data_path, "rb") as file:
                self.nodes[c_id][split] = pickle.load(file)

    def _gen_qa_pairs(self, c_id: str, cfg: Dict[str, Any]):
        persist_dir = os.path.join(os.environ["PERSIST_DIR"], c_id, "qa_pairs")
        os.makedirs(persist_dir, exist_ok=True)
        self.qa_pairs[c_id] = {}
        for split in ["train", "val", "test"]:
            print(f"Generating QA Pairs for {c_id}, {split}")
            self.qa_pairs[c_id][split] = generate_qa_embedding_pairs(
                llm=Settings.llm,
                nodes=self.nodes[c_id][split],
                num_questions_per_chunk=cfg.get("num_questions_per_chunk", 2),
            )
            data_path = os.path.join(persist_dir, f"qa_pairs_{split}.json")
            self.qa_pairs[c_id][split].save_json(data_path)

    def get_qa_pairs(self, c_id: str):
        self.qa_pairs[c_id] = {}
        persist_dir = os.path.join(os.environ["PERSIST_DIR"], c_id, "qa_pairs")
        for split in ["train", "val", "test"]:
            print(f"Loading QA pairs for {c_id}, {split}")
            data_path = os.path.join(persist_dir, f"qa_pairs_{split}.json")
            self.qa_pairs[c_id][split] = EmbeddingQAFinetuneDataset.from_json(
                data_path
            )

    def _set_engines(self, c_id: str):
        component_cfg = self.components_cfg[c_id]

        # Index is the main output, check if exists
        index_data_path = os.path.join(
            os.environ["PERSIST_DIR"], c_id, "indexes", "baseline"
        )
        if not os.path.exists(index_data_path):
            print(f"Generating engines for {c_id}")
            self.index[c_id] = self.gen_index(
                c_id,
                "baseline",
                self.nodes[c_id]["train"]
                + self.nodes[c_id]["val"]
                + self.nodes[c_id]["test"],
                component_cfg.get("gen_index", {}),
            )

        else:
            print(f"Loading engines for {c_id}")
            self.index[c_id] = self.get_index(c_id, "baseline")

        self.query_engine[c_id] = self.gen_query_engine(
            c_id, self.index[c_id], component_cfg.get("gen_query_engine", {})
        )
        self.query_engine_tools.append(
            self.gen_query_engine_tool(
                c_id,
                self.query_engine[c_id],
                component_cfg.get("gen_query_engine", {}),
            )
        )

    @staticmethod
    def gen_index(c_id: str, index_name: str, nodes, cfg: Dict[str, Any] = {}):
        print(f"Generating Index for {c_id}")
        persist_dir = os.path.join(
            os.environ["PERSIST_DIR"], c_id, "indexes", index_name
        )
        os.makedirs(persist_dir)

        ## From documents
        # self.index[c] = VectorStoreIndex.from_documents(
        #     documents=self.documents[c],
        #     **cfg,
        #     # service_context,  # Deprecated, use Settings
        #     # storage_context,
        #     # callback_manager,
        #     # transformations,
        # )

        ## From vector stores as storage_context
        # db = chromadb.PersistentClient(
        #     path=os.path.join(os.environ["WORKDIR"], "chroma_db")
        # )
        # chroma_collection = db.get_or_create_collection(name_collection)
        # vector_store = ChromaVectorStore(chroma_collection=chroma_collection)
        ## or
        # vector_store = PineconeVectorStore(pinecone.Index("quickstart"))
        # storage_context = StorageContext.from_defaults(
        #     vector_store=vector_store
        # )
        # self.index[c] = VectorStoreIndex.from_vector_store(
        #     vector_store=vector_store
        # )

        ## From nodes
        index = VectorStoreIndex(nodes, **cfg)

        ## Persist index to avoid constructing it again
        index.storage_context.persist(persist_dir)

        return index

    @staticmethod
    def get_index(c_id: str, index_name: str):
        print(f"Loading Index for {c_id}, {index_name}")
        persist_dir = os.path.join(
            os.environ["PERSIST_DIR"], c_id, "indexes", index_name
        )
        storage_context = StorageContext.from_defaults(persist_dir=persist_dir)
        index = load_index_from_storage(storage_context)
        return index

    @staticmethod
    def gen_retriever(c_id: str, index, cfg: Dict[str, Any] = {}):
        print(f"Generating Retriever for {c_id}")
        retriever = index.as_retriever(
            similarity_top_k=cfg.get("similarity_top_k", 3),
            ## You can set here  parameters for vector store and alpha
            # https://docs.llamaindex.ai/en/stable/api_reference/retrievers/vector/
        )
        return retriever

    @staticmethod
    def gen_query_engine(c_id: str, index, cfg: Dict[str, Any] = {}):
        print(f"Generating Query Engine for {c_id}")
        query_engine = index.as_query_engine(
            similarity_top_k=cfg.get("similarity_top_k", 3)
        )
        return query_engine

    @staticmethod
    def gen_query_engine_tool(
        c_id: str, query_engine, cfg: Dict[str, Any] = {}
    ):
        print(f"Generating Query Engine Tool for {c_id}")
        return QueryEngineTool(
            query_engine=query_engine,
            metadata=ToolMetadata(
                name=c_id,
                description="Input is a user query to obtain information."
                + cfg.get("description"),
            ),
        )

    @staticmethod
    def gen_chat_engine(c_id: str, index, cfg: Dict[str, Any] = {}):
        print(f"Generating Retriever for {c_id}")
        CHAT_SYSTEM_CONTENT = """
            Here are the relevant documents for the context:
            {context_str}
            ----
            Given the context information and not prior knowledge,
            answer to the question, as briefly as possible.
            Structure your response as a list of facts.
        """
        memory = ChatMemoryBuffer.from_defaults(token_limit=3900)

        chat_engine = index.as_chat_engine(
            similarity_top_k=cfg.get("similarity_top_k", 3),
            chat_mode="condense_plus_context",
            memory=memory,
            # llm=llm,
            context_prompt=CHAT_SYSTEM_CONTENT,
            verbose=False,
        )
        return chat_engine

    def _eval_data(self, c_id: str):
        # Index is the main output, check if exists
        persist_dir = os.path.join(
            os.environ["PERSIST_DIR"], c_id, "eval_data"
        )
        self.eval_data[c_id] = {}
        if not os.path.exists(persist_dir):
            os.makedirs(persist_dir, exist_ok=True)
            corpus = self.get_corpus(self.index[c_id])
            for split in ["train", "val", "test"]:
                print(f"Generating eval data for {c_id}, {split}")
                self.eval_data[c_id][split] = self.gen_eval_data(
                    self.query_engine[c_id],
                    self.qa_pairs[c_id][split],
                    corpus,
                )
                data_path = os.path.join(persist_dir, f"eval_data_{split}.pkl")
                self.eval_data[c_id][split].to_pickle(data_path)

        else:
            for split in ["train", "val", "test"]:
                print(f"Loading eval data for {c_id}, {split}")
                data_path = os.path.join(persist_dir, f"eval_data_{split}.pkl")
                self.eval_data[c_id][split] = pd.read_pickle(data_path)

    @staticmethod
    def get_corpus_from_index(index):
        return {dd.id_: dd.text for dd in index.docstore.docs.values()}

    @staticmethod
    def gen_eval_data(query_engine, qa_pairs, corpus):
        df_eval_dict = {}
        for q_id, query in tqdm(qa_pairs.queries.items()):
            reference_id = qa_pairs.relevant_docs[q_id][0]
            reference = qa_pairs.corpus[reference_id]  # can use corpus too

            response = query_engine.query(query)
            contexts_ids = [sn.id_ for sn in response.source_nodes]
            contexts = [corpus[n_id] for n_id in contexts_ids]
            df_eval_dict[q_id] = [
                query,
                reference_id,
                reference,
                contexts_ids,
                contexts,
                str(response),
            ]

        return pd.DataFrame.from_dict(
            df_eval_dict,
            orient="index",
            columns=[
                "query",
                "reference_id",
                "reference",
                "contexts_ids",
                "contexts",
                "response",
            ],
        )

    def execute(self):
        for c_id, _ in self.components_cfg.items():
            self._ingest_data(c_id)
            self._set_engines(c_id)
            self._eval_data(c_id)
