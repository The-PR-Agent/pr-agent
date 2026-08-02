import java.io.FileInputStream;
import java.io.IOException;

class DigestReader {
    byte[] read(String path) throws IOException {
        FileInputStream stream = new FileInputStream(path);
        return stream.readAllBytes();
    }
}
