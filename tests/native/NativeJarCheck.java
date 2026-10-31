import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.jar.Attributes;
import java.util.jar.JarFile;
import org.tensorflow.Result;
import org.tensorflow.SavedModelBundle;
import org.tensorflow.Session;
import org.tensorflow.Tensor;
import org.tensorflow.TensorFlow;
import org.tensorflow.ndarray.Shape;
import org.tensorflow.ndarray.buffer.DataBuffers;
import org.tensorflow.proto.ConfigProto;
import org.tensorflow.proto.DataType;
import org.tensorflow.proto.SignatureDef;
import org.tensorflow.types.TFloat32;
import org.tensorflow.types.TInt64;
import org.tensorflow.types.TString;

/** Loads the self-contained compatibility Jar using only the Java 8 runtime. */
public final class NativeJarCheck {
    private static final JsonSupport JSON = new JsonSupport();
    private static final String[] INPUTS = {"user_id", "slot_id", "feature1", "feature2", "text"};
    private static final int[] INPUT_LENGTHS = {3, 1, 3, 1, 1};
    private static final String[] OUTPUTS = {"pred", "pred_embedding", "pred_ids", "text_out"};
    private static final int[] OUTPUT_LENGTHS = {2, 4, 3, 1};
    private static final DataType[] OUTPUT_TYPES = {DataType.DT_FLOAT, DataType.DT_FLOAT,
            DataType.DT_INT64, DataType.DT_STRING};
    private static int comparisons;
    private static int batches;


    /** Test-only JSON support supplied by JDK 8; no Jackson in the runtime jar. */
    private static final class JsonNode {
        final Object value;
        JsonNode(Object value) { this.value = value; }
        JsonNode get(String key) {
            Object item = ((Map<?, ?>) value).get(key);
            return item == null ? null : new JsonNode(item);
        }
        JsonNode path(String key) { JsonNode item = get(key); return item == null ? new JsonNode(null) : item; }
        JsonNode get(int index) { return new JsonNode(((List<?>) value).get(index)); }
        int size() { return value instanceof List ? ((List<?>) value).size() : ((Map<?, ?>) value).size(); }
        boolean isArray() { return value instanceof List; }
        boolean isTextual() { return value instanceof String; }
        String textValue() { return (String) value; }
        long longValue() { return ((Number) value).longValue(); }
        int intValue() { return ((Number) value).intValue(); }
        float floatValue() { return ((Number) value).floatValue(); }
        double doubleValue() { return ((Number) value).doubleValue(); }
    }

    private static final class JsonSupport {
        private final javax.script.ScriptEngine engine;
        JsonSupport() {
            engine = new javax.script.ScriptEngineManager().getEngineByName("nashorn");
            if (engine == null) throw new IllegalStateException("The test harness requires the full JDK 8 Nashorn engine");
            try {
                engine.eval("function parseFixture(text) { return Java.asJSONCompatible(JSON.parse(text)); }");
            } catch (javax.script.ScriptException error) { throw new IllegalStateException(error); }
        }
        JsonNode readTree(java.io.File file) throws Exception {
            String text = new String(Files.readAllBytes(file.toPath()), java.nio.charset.StandardCharsets.UTF_8);
            return new JsonNode(((javax.script.Invocable) engine).invokeFunction("parseFixture", text));
        }
        String writeValueAsString(Object value) {
            if (value == null) return "null";
            if (value instanceof Number || value instanceof Boolean) return value.toString();
            if (value instanceof Map) {
                StringBuilder out = new StringBuilder("{");
                for (Map.Entry<?, ?> entry : ((Map<?, ?>) value).entrySet()) {
                    if (out.length() > 1) out.append(',');
                    out.append(writeValueAsString(entry.getKey().toString())).append(':')
                            .append(writeValueAsString(entry.getValue()));
                }
                return out.append('}').toString();
            }
            if (value instanceof Iterable) {
                StringBuilder out = new StringBuilder("[");
                for (Object item : (Iterable<?>) value) {
                    if (out.length() > 1) out.append(',');
                    out.append(writeValueAsString(item));
                }
                return out.append(']').toString();
            }
            StringBuilder out = new StringBuilder();
            out.append((char) 34);
            for (char ch : value.toString().toCharArray()) {
                if (ch == 34 || ch == 92) out.append((char) 92).append(ch);
                else if (ch < 32) out.append(String.format("%c%c%04x", 92, 'u', (int) ch));
                else out.append(ch);
            }
            return out.append((char) 34).toString();
        }
    }

    private static void require(boolean condition, String message) {
        if (!condition) throw new AssertionError(message);
    }

    private static String sha256(Path path) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        byte[] buffer = new byte[1024 * 1024];
        try (InputStream stream = Files.newInputStream(path)) {
            int count;
            while ((count = stream.read(buffer)) >= 0) digest.update(buffer, 0, count);
        }
        StringBuilder result = new StringBuilder();
        for (byte value : digest.digest()) result.append(String.format("%02x", value & 255));
        return result.toString();
    }

    private static Tensor input(JsonNode rows, int offset, int count, int feature) {
        int length = INPUT_LENGTHS[feature];
        Shape shape = Shape.of(count, length);
        if (feature < 2) {
            long[] values = new long[count * length];
            for (int row = 0; row < count; row++) {
                JsonNode items = rows.get(offset + row).get(INPUTS[feature]);
                require(items != null && items.size() == length, "Invalid encoded input " + INPUTS[feature]);
                for (int column = 0; column < length; column++) values[row * length + column] = items.get(column).longValue();
            }
            return TInt64.tensorOf(shape, DataBuffers.of(values));
        }
        if (feature < 4) {
            float[] values = new float[count * length];
            for (int row = 0; row < count; row++) {
                JsonNode items = rows.get(offset + row).get(INPUTS[feature]);
                require(items != null && items.size() == length, "Invalid encoded input " + INPUTS[feature]);
                for (int column = 0; column < length; column++) values[row * length + column] = items.get(column).floatValue();
            }
            return TFloat32.tensorOf(shape, DataBuffers.of(values));
        }
        String[] values = new String[count];
        for (int row = 0; row < count; row++) {
            JsonNode items = rows.get(offset + row).get(INPUTS[feature]);
            require(items != null && items.size() == 1 && items.get(0).isTextual(), "Invalid encoded string input");
            values[row] = items.get(0).textValue();
        }
        return TString.tensorOf(shape, DataBuffers.ofObjects(values));
    }

    private static void closeTo(double expected, float actual, String where) {
        comparisons++;
        require(!Float.isNaN(actual) && !Float.isInfinite(actual)
                && Math.abs(expected - actual) <= 1e-5 + 1e-6 * Math.abs(expected),
                where + ": expected " + expected + ", got " + actual);
    }

    private static void compare(Result result, JsonNode golden, int offset, int count, String where) {
        require(result.size() == OUTPUTS.length, where + ": output count");
        for (int output = 0; output < OUTPUTS.length; output++) {
            Tensor tensor = result.get(output);
            require(tensor.dataType() == OUTPUT_TYPES[output], where + ": dtype for " + OUTPUTS[output]);
            require(Arrays.equals(tensor.shape().asArray(), new long[] {count, OUTPUT_LENGTHS[output]}),
                    where + ": shape for " + OUTPUTS[output] + " is " + tensor.shape());
        }
        for (int row = 0; row < count; row++) {
            JsonNode expected = golden.get(offset + row);
            require(expected.path("id").intValue() == offset + row + 1, "Golden rows must preserve source order");
            closeTo(expected.get("pctr").doubleValue(), ((TFloat32) result.get(0)).getFloat(row, 0), where + "/pred[0]");
            closeTo(expected.get("pcvr").doubleValue(), ((TFloat32) result.get(0)).getFloat(row, 1), where + "/pred[1]");
            for (int column = 0; column < 4; column++) {
                closeTo(expected.get("embedding").get(column).doubleValue(),
                        ((TFloat32) result.get(1)).getFloat(row, column), where + "/embedding");
            }
            for (int column = 0; column < 3; column++) {
                comparisons++;
                require(expected.get("ids").get(column).longValue() == ((TInt64) result.get(2)).getLong(row, column),
                        where + "/ids row=" + (offset + row) + " column=" + column);
            }
            comparisons++;
            require(expected.get("text").textValue().equals(((TString) result.get(3)).getObject(row, 0)),
                    where + "/text row=" + (offset + row));
        }
    }

    private static void verify(Path fixtures, String model, String goldenName, JsonNode encoded,
                               String expectedRuntime) throws Exception {
        JsonNode golden = JSON.readTree(fixtures.resolve(goldenName).toFile());
        require(golden.isArray() && golden.size() == 24, "Expected 24 golden rows for " + model);
        ConfigProto options = ConfigProto.newBuilder().setIntraOpParallelismThreads(1)
                .setInterOpParallelismThreads(1).build();
        try (SavedModelBundle bundle = SavedModelBundle.loader(fixtures.resolve(model).toString())
                .withTags("serve").withConfigProto(options).load()) {
            require(expectedRuntime.equals(bundle.metaGraphDef().getMetaInfoDef().getTensorflowVersion()),
                    "SavedModel producer version for " + model);
            SignatureDef signature = bundle.metaGraphDef().getSignatureDefOrThrow("serving_default");
            require(signature.getInputsCount() == INPUTS.length && signature.getOutputsCount() == OUTPUTS.length,
                    "Unexpected serving signature for " + model);
            for (int size : new int[] {1, 3, 7}) {
                for (int offset = 0; offset < encoded.size(); offset += size) {
                    int count = Math.min(size, encoded.size() - offset);
                    List<Tensor> tensors = new ArrayList<>();
                    try {
                        Session.Runner runner = bundle.session().runner();
                        for (int feature = 0; feature < INPUTS.length; feature++) {
                            Tensor tensor = input(encoded, offset, count, feature);
                            tensors.add(tensor);
                            runner.feed(signature.getInputsOrThrow(INPUTS[feature]).getName(), tensor);
                        }
                        for (String output : OUTPUTS) runner.fetch(signature.getOutputsOrThrow(output).getName());
                        try (Result result = runner.run()) {
                            compare(result, golden, offset, count, model + "/batch=" + size + "/offset=" + offset);
                        }
                        batches++;
                    } finally {
                        for (Tensor tensor : tensors) tensor.close();
                    }
                }
            }
        }
    }

    public static void main(String[] args) throws Exception {
        require(args.length == 3, "Usage: NativeJarCheck FIXTURES RUNTIME_VERSION JAVA_API_VERSION");
        Path fixtures = Paths.get(args[0]);
        String expectedRuntime = args[1];
        require(System.getProperty("java.version").startsWith("1.8."), "The native compatibility check requires Java 8");
        Path jar = Paths.get(TensorFlow.class.getProtectionDomain().getCodeSource().getLocation().toURI());
        try (JarFile archive = new JarFile(jar.toFile())) {
            Attributes manifest = archive.getManifest().getMainAttributes();
            require(args[2].equals(manifest.getValue("TensorFlow-Java-Version")), "Wrong TensorFlow Java Manifest version");
            require(expectedRuntime.equals(manifest.getValue("TensorFlow-Runtime-Version")), "Wrong TensorFlow runtime Manifest version");
        }
        JsonNode producer = JSON.readTree(fixtures.resolve("producer.json").toFile());
        require(expectedRuntime.equals(producer.path("tensorflow").textValue()), "Stale fixture producer version");
        JsonNode encoded = JSON.readTree(fixtures.resolve("encoded.json").toFile());
        require(encoded.isArray() && encoded.size() == 24, "Expected all 24 pre-encoded fixture rows");
        String runtime = TensorFlow.version();
        require(expectedRuntime.equals(runtime), "Wrong native runtime: " + runtime);
        verify(fixtures, "modeldir", "golden.json", encoded, expectedRuntime);
        verify(fixtures, "legacy_modeldir", "legacy_golden.json", encoded, expectedRuntime);
        require(batches == 72 && comparisons == 1440, "Incomplete native model coverage");
        Map<String, Object> report = new LinkedHashMap<>();
        report.put("status", "passed");
        report.put("java_version", System.getProperty("java.version"));
        report.put("os_arch", System.getProperty("os.arch"));
        report.put("tensorflow_java", args[2]);
        report.put("tensorflow_runtime", runtime);
        report.put("jar_sha256", sha256(jar));
        report.put("models", Arrays.asList("modeldir", "legacy_modeldir"));
        report.put("rows_per_model", 24);
        report.put("batch_sizes", Arrays.asList(1, 3, 7));
        report.put("batches", batches);
        report.put("scalar_comparisons", comparisons);
        report.put("float_absolute_tolerance", 1e-5);
        report.put("float_relative_tolerance", 1e-6);
        System.out.println("NATIVE_CHECK_JSON=" + JSON.writeValueAsString(report));
    }
}
