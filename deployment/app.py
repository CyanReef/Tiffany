"""Application module executed by the prepared, isolated interpreter."""
from application import TiffanyApplication

if __name__ == "__main__":
    raise SystemExit(TiffanyApplication.run())
