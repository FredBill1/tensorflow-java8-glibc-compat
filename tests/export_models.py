"""Export TF 2.21 Keras fixtures, encoded inputs, checkpoints, and goldens.

The default entry point runs Keras 3 and legacy Keras in separate processes.
No TensorFlow import takes place until the child has selected its Keras mode.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent / "fixtures"
TF_VERSION = "2.21.0"
KERAS_VERSION = "3.15.1"
TF_KERAS_VERSION = "2.21.0"
SEED = 20261030
REQUIRED_OPS = ["RaggedFillEmptyRows", "StatelessShuffle"]
MODEL_NAMES = {"modeldir", "legacy_modeldir"}


def dataset():
    metadata = {
        "dense": {"feature1": {"mean": [0., 1., 2.], "std": [1., 2., 4.]},
                  "feature2": {"mean": [1.], "std": [2.]}},
        "sparse": {"user_id": {"u1": 2, "u2": 3, "u3": 4}, "slot_id": {"s1": 2, "s2": 3}},
    }
    rows, encoded = [], []
    for i in range(1, 25):
        users = ["u1,u2", "", "u1,,unknown", None, "u3,u2,u1,u3"][i % 5]
        slot = "s1" if i % 2 else "missing"
        text = None if i % 3 == 0 else "用户{}".format(i)
        raw = [float(i), float(i + 1), float(i + 2)]
        ids = [0, 0, 0]
        if users:
            ids = [metadata["sparse"]["user_id"].get(value, 1) if value else 1
                   for value in users.split(",")][:3]
            ids += [0] * (3 - len(ids))
        encoded.append({"user_id": ids,
                        "slot_id": [metadata["sparse"]["slot_id"].get(slot, 1)],
                        "feature1": [(value - mean) / std for value, mean, std in
                                     zip(raw, [0., 1., 2.], [1., 2., 4.])],
                        "feature2": [(i + 2. - 1.) / 2.], "text": [text or ""]})
        rows.append({"id": i, "user_id": users, "slot_id": slot,
                     "feature1": ",".join(map(str, raw)), "feature2": i + 2., "text": text,
                     "pred": "source-pred-must-not-leak", "nullable_value": None})
    return metadata, rows, encoded


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def remove_model(name):
    """Only replace model directories owned by this fixture exporter."""
    if name not in MODEL_NAMES:
        raise ValueError("Unknown fixture model: " + name)
    path = ROOT / name
    if path.is_symlink() or path.resolve().parent != ROOT.resolve():
        raise ValueError("Fixture model must remain a direct, non-symlink child: " + str(path))
    if path.exists():
        shutil.rmtree(path)
    return path


def reachable_ops(function):
    graph = function.graph.as_graph_def()
    functions = {item.signature.name: item for item in graph.library.function}
    visited, operations = set(), set()
    pending = list(graph.node)
    while pending:
        node = pending.pop()
        operations.add(node.op)
        references = [node.op]
        for attribute in node.attr.values():
            if attribute.HasField("func"):
                references.append(attribute.func.name)
            references.extend(item.name for item in attribute.list.func)
        for name in references:
            if name in functions and name not in visited:
                visited.add(name)
                pending.extend(functions[name].node_def)
    return sorted(operations)


def verify_numeric_outputs(np, inputs, outputs, weights):
    """Check serving arithmetic independently of the TensorFlow raw ops."""
    users = inputs["user_id"]
    shuffled = outputs["pred_ids"] - inputs["slot_id"]
    np.testing.assert_array_equal(np.sort(shuffled, axis=1), np.sort(users, axis=1))
    assert np.any(shuffled != users), "StatelessShuffle must affect the observable ID output"
    empty_rows = ~np.any(users != 0, axis=1)
    assert np.any(empty_rows) and np.any(~empty_rows), "Exercise both ragged row paths"
    ragged_sums = np.where(empty_rows, .5, np.sum(users, axis=1)).astype(np.float32)[:, None]
    dense = np.concatenate([inputs["feature1"], inputs["feature2"]], axis=1)
    expected = dense @ weights[0] + weights[1]
    expected += np.sum(shuffled.astype(np.float32) * np.array([[.01, .02, .03]], np.float32),
                       axis=1, keepdims=True)
    expected += inputs["slot_id"].astype(np.float32) * .01 + ragged_sums * .001
    np.testing.assert_allclose(outputs["pred"], np.concatenate([expected, expected * 2], axis=1),
                               rtol=1e-6, atol=1e-6)


def export_child(flavor):
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
    os.environ["TF_CPP_MIN_LOG_LEVEL"] = "2"
    os.environ["TF_NUM_INTRAOP_THREADS"] = "1"
    os.environ["TF_NUM_INTEROP_THREADS"] = "1"
    os.environ["KERAS_BACKEND"] = "tensorflow"
    if flavor == "legacy":
        os.environ["TF_USE_LEGACY_KERAS"] = "1"
    else:
        os.environ.pop("TF_USE_LEGACY_KERAS", None)

    import numpy as np
    import tensorflow as tf
    if tf.__version__ != TF_VERSION:
        raise RuntimeError("Expected TensorFlow {}, got {}".format(TF_VERSION, tf.__version__))
    keras = tf.keras
    if flavor == "legacy":
        import tf_keras
        keras_version = tf_keras.__version__
        assert keras.Model.__module__.startswith("tf_keras."), keras.Model.__module__
        assert keras_version == TF_KERAS_VERSION, keras_version
    else:
        import keras as standalone_keras
        keras_version = standalone_keras.__version__
        assert keras.Model.__module__.startswith("keras."), keras.Model.__module__
        assert keras_version == KERAS_VERSION, keras_version
    keras.utils.set_random_seed(SEED)

    signature = [tf.TensorSpec([None, 3], tf.int64, "user_id"),
                 tf.TensorSpec([None, 1], tf.int64, "slot_id"),
                 tf.TensorSpec([None, 3], tf.float32, "feature1"),
                 tf.TensorSpec([None, 1], tf.float32, "feature2"),
                 tf.TensorSpec([None, 1], tf.string, "text")]
    _, rows, encoded = dataset()
    dtypes = {item.name: item.dtype for item in signature}
    inputs = {name: tf.constant([row[name] for row in encoded], dtype)
              for name, dtype in dtypes.items()}

    # Train a numerical Keras head separately, so serving outputs retain their
    # mixed dtypes without inventing losses for strings or IDs.
    head = keras.Sequential([
        keras.layers.Input(shape=(4,), dtype="float32"),
        keras.layers.Dense(1, kernel_initializer=keras.initializers.Ones(),
                           bias_initializer=keras.initializers.Constant(0.5)),
    ], name="trained_dense_head")
    head.compile(optimizer=keras.optimizers.SGD(learning_rate=0.01), loss="mse", jit_compile=False)
    train_x = np.array([[0., 1., 2., 3.], [1., -1., .5, 2.], [-1., 0., 1., .5],
                        [2., .5, -1., 0.]], dtype=np.float32)
    train_y = train_x @ np.array([[.75], [.5], [.25], [.125]], dtype=np.float32) + .25
    initial_weights = [value.copy() for value in head.get_weights()]
    losses = [float(np.asarray(head.train_on_batch(train_x, train_y)).reshape(-1)[0]) for _ in range(5)]
    assert any(not np.array_equal(before, after) for before, after in zip(initial_weights, head.get_weights()))

    class PredictionLayer(keras.layers.Layer):
        def __init__(self):
            super().__init__(name="fixture_" + flavor)
            self.head = head

        def call(self, values, training=False):
            user_id, slot_id = values["user_id"], values["slot_id"]
            feature1, feature2, text = values["feature1"], values["feature2"], values["text"]
            shuffled = tf.map_fn(
                lambda row: tf.raw_ops.StatelessShuffle(
                    value=row, key=tf.constant([SEED], tf.uint64),
                    counter=tf.constant([0, 0], tf.uint64), alg=tf.constant(1, tf.int32)),
                user_id, fn_output_signature=tf.TensorSpec([3], tf.int64))
            indices = tf.where(user_id != 0)
            filled_rowids, filled_values, _, _ = tf.raw_ops.RaggedFillEmptyRows(
                value_rowids=indices[:, 0], values=tf.cast(tf.gather_nd(user_id, indices), tf.float32),
                nrows=tf.shape(user_id, out_type=tf.int64)[0], default_value=tf.constant(.5, tf.float32))
            ragged_score = tf.math.unsorted_segment_sum(
                filled_values, filled_rowids, tf.shape(user_id)[0])[:, None]
            dense = tf.concat([feature1, feature2], axis=1)
            score = self.head(dense, training=False)
            score += tf.reduce_sum(tf.cast(shuffled, tf.float32) * [[.01, .02, .03]], axis=1, keepdims=True)
            score += tf.cast(slot_id, tf.float32) * .01 + ragged_score * .001
            return {"pred": tf.concat([score, score * 2], axis=1), "pred_embedding": dense,
                    "pred_ids": shuffled + slot_id,
                    "text_out": tf.strings.join([text, tf.fill(tf.shape(text), "✓")])}

    def export_model(name, model, required, golden_file=None):
        path = remove_model(name)
        if flavor == "keras3":
            archive = keras.export.ExportArchive()
            archive.track(model)
            archive.add_endpoint(name="serving_default", fn=model.serve)
            archive.write_out(str(path), verbose=False)
        else:
            model.save(str(path), save_format="tf", include_optimizer=False,
                       signatures={"serving_default": model.serve})
        loaded = tf.saved_model.load(str(path))
        function = loaded.signatures["serving_default"]
        operations = reachable_ops(function)
        assert set(required).issubset(operations), (name, required, operations)
        assert not {"PyFunc", "PyFuncStateless", "EagerPyFunc"}.intersection(operations), (name, operations)
        info = {"keras_flavor": flavor, "keras_version": keras_version,
                "keras_model_module": keras.Model.__module__,
                "TF_USE_LEGACY_KERAS": os.environ.get("TF_USE_LEGACY_KERAS"),
                "required_ops": required, "reachable_ops": operations,
                "saved_model_sha256": hashlib.sha256((path / "saved_model.pb").read_bytes()).hexdigest()}
        if golden_file:
            info["golden"] = golden_file
        return function, info

    # Functional inputs retain each mixed dtype when Keras creates additional
    # SavedModel functions. A subclassed Model would infer one default float
    # dtype for every field during automatic signature tracing.
    keras_inputs = {spec.name: keras.layers.Input(shape=spec.shape[1:], dtype=spec.dtype, name=spec.name)
                    for spec in signature}
    model = keras.Model(keras_inputs, PredictionLayer()(keras_inputs), name="fixture_" + flavor)

    @tf.function(input_signature=signature)
    def serve_prediction(user_id, slot_id, feature1, feature2, text):
        return model({"user_id": user_id, "slot_id": slot_id, "feature1": feature1,
                      "feature2": feature2, "text": text}, training=False)

    model.serve = serve_prediction
    direct = model(inputs, training=False)
    name = "modeldir" if flavor == "keras3" else "legacy_modeldir"
    golden_file = "golden.json" if flavor == "keras3" else "legacy_golden.json"
    serving, model_info = export_model(name, model, REQUIRED_OPS, golden_file)
    restored = serving(**inputs)
    verify_numeric_outputs(np, {key: value.numpy() for key, value in inputs.items()},
                           {key: value.numpy() for key, value in restored.items()}, head.get_weights())
    for key in direct:
        if direct[key].dtype == tf.float32:
            np.testing.assert_allclose(restored[key].numpy(), direct[key].numpy(), rtol=1e-6, atol=1e-6)
        else:
            np.testing.assert_array_equal(restored[key].numpy(), direct[key].numpy())
    # Changing batch partitions must not change row-level shuffles or empty-row
    # filling. Native tests use another batch size again.
    for batch_size in (1, 3, 7):
        chunked = {key: [] for key in restored}
        for start in range(0, len(rows), batch_size):
            result = serving(**{key: value[start:start + batch_size] for key, value in inputs.items()})
            for key, value in result.items():
                chunked[key].append(value.numpy())
        for key, values in chunked.items():
            values = np.concatenate(values)
            if restored[key].dtype == tf.float32:
                np.testing.assert_allclose(values, restored[key].numpy(), rtol=1e-6, atol=1e-6)
            else:
                np.testing.assert_array_equal(values, restored[key].numpy())
    goldens = [{"id": row["id"], "pctr": float(restored["pred"].numpy()[index, 0]),
                "pcvr": float(restored["pred"].numpy()[index, 1]),
                "embedding": restored["pred_embedding"].numpy()[index].tolist(),
                "ids": restored["pred_ids"].numpy()[index].tolist(),
                "text": restored["text_out"].numpy()[index, 0].decode("utf-8")}
               for index, row in enumerate(rows)]
    write_json(ROOT / golden_file, goldens)
    models = {name: model_info}
    write_json(ROOT / ("producer-" + flavor + ".json"),
               {"tensorflow": tf.__version__, "python": sys.version.split()[0], "models": models,
                "training": {"seed": SEED, "steps": 5, "optimizer": "SGD", "learning_rate": .01,
                             "jit_compile": False, "losses": losses,
                             "weights": [value.tolist() for value in head.get_weights()]}})
    print("Exported {} fixtures with TensorFlow {} / Keras {}".format(flavor, tf.__version__, keras_version))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keras-flavor", choices=("keras3", "legacy"), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Export fixtures with Python 3.12 and tests/model-requirements.lock")
    if ROOT.is_symlink() or ROOT.is_junction() or ROOT.resolve().parent != ROOT.parent.resolve():
        raise SystemExit("tests/fixtures must be a real directory within this checkout")
    ROOT.mkdir(exist_ok=True)
    if args.keras_flavor:
        export_child(args.keras_flavor)
        return
    (ROOT / "producer.json").unlink(missing_ok=True)
    metadata, rows, encoded = dataset()
    write_json(ROOT / "metadata.json", metadata)
    write_json(ROOT / "encoded.json", encoded)
    (ROOT / "source.json").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
    models, training = {}, {}
    for flavor in ("keras3", "legacy"):
        subprocess.run([sys.executable, str(Path(__file__).resolve()), "--keras-flavor", flavor], check=True)
        fragment_path = ROOT / ("producer-" + flavor + ".json")
        fragment = json.loads(fragment_path.read_text(encoding="utf-8"))
        assert fragment["tensorflow"] == TF_VERSION
        models.update(fragment["models"])
        training[flavor] = fragment["training"]
        fragment_path.unlink()
    write_json(ROOT / "producer.json", {"schema_version": 1, "tensorflow": TF_VERSION,
               "python": sys.version.split()[0], "required_ops": REQUIRED_OPS,
               "row_count": len(rows), "models": models, "training": training})
    print("Validated {} Keras fixtures and both goldens in {}".format(len(models), ROOT))


if __name__ == "__main__":
    main()
