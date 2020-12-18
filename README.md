# mtriage-viewer

#### note: pre-alpha development, not ready for use. Everything will break!

mtriage-server takes a folder of passes produced by
[mtriage](https://github.com/forensic-architecture/mtriage), which can be
either local or remote in something like AWS S3, and makes them available via
a simple set of API endpoints. 

See [mtriage-viewer](https://github.com/forensic-architecture/mtriage-viewer)
for a detailed example of how to consume mtriage-server's endpoints.

## Run 
```
cd server
poetry install
poetry shell
export FLASK_ENV=development
python app.py
```
