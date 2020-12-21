import os
import re
import json
import boto3  # s3 client
import pickle
from enum import Enum
from typing import List
from types import SimpleNamespace
from pathlib import Path
from abc import ABC, abstractmethod
from configparser import RawConfigParser
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)


class StorageType(Enum):
    Local = 1
    S3 = 2


ROOT = "/sata/mtriage"  # TWEAK ME
STORAGE_TYPE = StorageType.Local

# TODO: pass as CLI args
# ROOT = "lk-iceland-personal"
# STORAGE_TYPE = StorageType.S3

EM_STORE = "./batches/map.pkl"


def read_etype(local_fp: Path) -> str:
    cfgParser = RawConfigParser()
    cfgParser.read(local_fp)
    return cfgParser.get("etype", "etype")


class Batch(ABC):
    def __init__(self, query, etype, root, elements=None):
        self.query = query
        self.etype = etype
        self.root = root
        if elements is not None:
            self.elements = elements
        else:
            self.elements = self.index_elements()

    def serialize(self):
        return {
            "query": self.query,
            "elements": [str(Path(x).name) for x in self.elements],
            "etype": self.etype,
        }

    def get(self, attr):
        return self.__dict__.get(attr)

    @abstractmethod
    def index_elements(self):
        pass

    @staticmethod
    def attrs():
        return ["query", "etype", "elements", "root"]


class LocalBatch(Batch):
    def __init__(self, query, etype, root, elements=None):
        super().__init__(query, etype, root, elements)
        self.elements = self.index_elements()

    def index_elements(self):
        els = [x for x in self.root.glob("**/*") if x.is_dir()]

        if any(re.match(".*\_\_RANKING", str(line.name)) for line in els):
            print('setting ranking...')
            self.ranking = self.unpack_element(Path(self.root/"__RANKING"))["media"]["rankings.json"]

        return els

    @staticmethod
    def unpack_element(pth: Path, suffixes: List[str] = [".json"]) -> dict:
        media = {}
        for f in [t for t in pth.iterdir() if t.suffix in suffixes]:
            with open(f) as fl:
                data = json.load(fl)
            media[f.name] = data

        return {
            "id": pth.name,
            "media": media,
        }

    def get_element(self, el_id: str):
        matching = [el for el in self.elements if el.name == el_id]
        if len(matching) != 1:
            return None
        return LocalBatch.unpack_element(matching[0])

    def get_elements(self, page=0, limit=10):
        return [LocalBatch.unpack_element(el) for el in self.elements]


class S3Batch(Batch):
    def __init__(self, query, etype, root, ranking={}, elements=None):
        self.ranking = ranking  # optionally set in `index_elements`
        super().__init__(query, etype, root, elements=elements)

    def index_elements(self):
        response = boto3.client("s3").list_objects_v2(
            Bucket=self.root, Prefix=self.query, Delimiter="/"
        )
        els = [x.get("Prefix") for x in response.get("CommonPrefixes")]

        if any(re.match(".*\_\_RANKING", line) for line in els):
            self.ranking = self.unpack_element("__RANKING")["media"]["rankings.json"]

        return els

    def unpack_element(self, el: str, suffixes: List[str] = [".json"]) -> dict:
        prefix = el if el.startswith(self.query) else f"{self.query}{el}"
        elpaths = [
            x.key
            for x in boto3.resource("s3")
            .Bucket(self.root)
            .objects.filter(Prefix=prefix)
            if Path(x.key).suffix in suffixes
        ]

        media = {}
        for elpath in elpaths:
            content_object = boto3.resource("s3").Object(self.root, elpath)
            file_content = content_object.get().get("Body").read().decode("utf-8")
            json_content = json.loads(file_content)
            media[Path(elpath).name] = json_content

        return {
            "id": Path(el).name,
            "media": media,
        }

    def get_element(self, el_id: str):
        matching = [el for el in self.elements if Path(el).name == el_id]
        if len(matching) != 1:
            return None
        return self.unpack_element(matching[0])

    def get_elements(self, page=0, limit=10, rank_by=None):
        start_idx = page * limit
        end_idx = start_idx + (limit - 1)

        els_to_give = self.elements[start_idx:end_idx]
        if rank_by is not None and rank_by in self.ranking:
            rank = self.ranking[rank_by]
            els_to_give = [el for el in rank[start_idx:end_idx]]

        return [self.unpack_element(el) for el in els_to_give]

    @staticmethod
    def attrs():
        return Batch.attrs() + ["ranking"]


def save_map(mp):
    with open(EM_STORE, "wb") as fp:
        pickle.dump([(x.__class__.__name__, x.__dict__) for x in mp["batches"]], fp)


def load_map():
    with open(EM_STORE, "rb") as fp:
        raw = pickle.load(fp)

    if len(raw) > 0:
        # NB: reconstructs class and its init args from the file on disk

        batches = [
            (
                globals()[typ],  # constructor, e.g. LocalBatch
                {k: dct[k] for k in globals()[typ].attrs()},
            )
            for (typ, dct) in raw
        ]

        return {"batches": [Batch(**args) for (Batch, args) in batches]}
    return {"batches": []}


class Local:
    @staticmethod
    def get_batches(root: str) -> List[LocalBatch]:
        batches = []
        for _, dirs, _ in os.walk(root):
            for d in dirs:
                absp = Path(root) / d
                if (absp / ".mtbatch").is_file():
                    etype = read_etype(absp / ".mtbatch")
                    batches.append(LocalBatch(d, etype, absp))
        return batches


class S3:
    @staticmethod
    def get_batches(root: str) -> List[S3Batch]:
        s3 = boto3.client("s3")
        s3_resource = boto3.resource("s3")
        # paginator = s3.get_paginator('list_objects')
        # result = paginator.paginate(Bucket=root, Delimiter='/')
        # folders = [prefix.get('Prefix') for prefix in result.search('CommonPrefixes')]
        all_objects = s3.list_objects(Bucket=root)
        valid_folders = [
            x["Key"].replace(".mtbatch", "")
            for x in all_objects["Contents"]
            if re.match(r".*\/\.mtbatch$", x["Key"])
        ]

        # download .mtbatch files to get etype
        batches = []
        bucket = s3_resource.Bucket(root)
        mtbatches_dir = Path("mtbatches")
        mtbatches_dir.mkdir(parents=True, exist_ok=True)

        for fold in valid_folders:
            local_fp = mtbatches_dir / fold.replace("/", "_")
            bucket.download_file(f"{fold}.mtbatch", str(local_fp))
            etype = read_etype(local_fp)
            batches.append(S3Batch(fold, etype, root))

        return batches


def index(root: str, storage_type: StorageType):
    """
    Runs on server start, indexing the Storage.
    ELEMENT_MAP is kept in memory from there.
    Specific batches are worked out dynamically.
    Simplistically, this function identifies all element batches inside the
    storage, reads the presumed etype, and presents an overview of available
    batches.
    """
    get_batches = {
        StorageType.Local: Local.get_batches,
        StorageType.S3: S3.get_batches,
    }.get(storage_type, lambda _: [])

    return {
        "batches": get_batches(root),
    }


def batch_from_query(batches, query):
    if not query:
        return None
    matching = [b for b in batches if b.query.strip("/") == query.strip("/")]
    if len(matching) != 1:
        return None
    return matching[0]


@app.route("/elementmap")
def elementmap():
    mp = load_map()
    return jsonify([x.serialize() for x in mp["batches"]])


@app.route("/batch", methods=["GET", "POST"])
def batch():
    mp = load_map()
    batches = mp["batches"]

    if request.method == "GET":
        arg_query = request.args.get("q")
        batch = batch_from_query(batches, arg_query)
        arg_element = request.args.get("el")

        arg_limit = request.args.get("limit")
        arg_limit = 10 if arg_limit is None else int(arg_limit)

        arg_page = request.args.get("page")
        arg_page = 0 if arg_page is None else int(arg_page)

        rank_by = request.args.get("rank_by")
        if rank_by is None:
            rank_by = "tank"

        if arg_element is not None:
            return jsonify(batch.get_element(arg_element))

        return jsonify(
            batch.get_elements(page=arg_page, limit=arg_limit, rank_by=rank_by)
        )
    else:  # POST
        data = request.json
        q = data.get("query")
        elements = data.get("elements")
        batch = batch_from_query(batches, q)
        return jsonify([batch.get_element(el) for el in elements])


@app.route("/batch_attribute")
def batch_attribute():
    """
    Get an attribute on all batches, or a single batch.
    Specify the name of the attribute with `a`.
    Specify the particular batch with `batch` (if blank will return attributes for all batches).
    """
    mp = load_map()
    q = request.args.get("q")
    attr = request.args.get("a")
    if q is None:
        return jsonify([x.get(attr) for x in mp["batches"]])
    try:
        stripped_q = q.strip("/")
        import pdb; pdb.set_trace()
        batch = next((b for b in mp["batches"] if b.query.strip("/") == stripped_q))
        return jsonify(batch.get(attr))
    except:
        return jsonify(None)


@app.route("/")
def fallback_route():
    return jsonify({"api": "v0.1"})


if __name__ == "__main__":
    mp = index(ROOT, STORAGE_TYPE)
    if len(mp["batches"]) > 0:
        save_map(mp)
    app.run(host="0.0.0.0")
