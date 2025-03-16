"""
A custom node for ComfyUI that serves contents from input and output folders
with real-time file change notifications via WebSockets.
"""

import importlib.util
import subprocess
import sys
import os

# Check if watchdog is installed
if importlib.util.find_spec("watchdog") is None:
    print("Installing watchdog module...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "watchdog>=3.0.0"])

import json
import shutil
import time
import asyncio
import threading
from aiohttp import web, WSCloseCode
import server
from server import PromptServer
from PIL import Image
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Store WebSocket connections and their subscriptions
websocket_connections = {}
file_watchers = {}

# File event handler class
class FileChangeHandler(FileSystemEventHandler):
    def __init__(self, folder_type, folder_path):
        self.folder_type = folder_type
        self.folder_path = folder_path
        self.base_path_length = len(folder_path) + 1  # +1 for the trailing slash
    
    def get_relative_path(self, path):
        if path.startswith(self.folder_path):
            return path[self.base_path_length:]
        return path
    
    def on_created(self, event):
        if event.is_directory:
            event_type = "directory_created"
        else:
            event_type = "file_created"
        self.notify_clients(event_type, event.src_path)
    
    def on_deleted(self, event):
        if event.is_directory:
            event_type = "directory_deleted"
        else:
            event_type = "file_deleted"
        self.notify_clients(event_type, event.src_path)
    
    def on_modified(self, event):
        if not event.is_directory:
            self.notify_clients("file_modified", event.src_path)
    
    def on_moved(self, event):
        if event.is_directory:
            event_type = "directory_moved"
        else:
            event_type = "file_moved"
        self.notify_clients(event_type, event.src_path, event.dest_path)
    
    def notify_clients(self, event_type, src_path, dest_path=None):
        relative_src = self.get_relative_path(src_path)
        relative_dest = self.get_relative_path(dest_path) if dest_path else None
        
        message = {
            "event": event_type,
            "folder_type": self.folder_type,
            "path": relative_src
        }
        
        if dest_path:
            message["destination"] = relative_dest
        
        # Send to all subscribed WebSocket connections
        asyncio.run_coroutine_threadsafe(
            broadcast_event(self.folder_type, message), 
            asyncio.get_event_loop()
        )

# Broadcast events to subscribed clients
async def broadcast_event(folder_type, message):
    for ws, subscriptions in list(websocket_connections.items()):
        if folder_type in subscriptions:
            try:
                await ws.send_json(message)
            except Exception:
                # Remove dead connections
                if ws in websocket_connections:
                    del websocket_connections[ws]

# Define the class for the custom node
class FolderServerNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "folder_type": (["input", "output"], "The folder to serve (input or output)"),
                "watch_for_changes": (["True", "False"], "Enable real-time file change notifications"),
            },
            "optional": {
                "subfolder": ("STRING", {"default": "", "multiline": False}),
            }
        }
    
    RETURN_TYPES = ("STRING",)
    FUNCTION = "serve_folder"
    CATEGORY = "utils"
    OUTPUT_NODE = True

    def serve_folder(self, folder_type, watch_for_changes, subfolder=""):
        folder_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), folder_type)
        if subfolder:
            folder_path = os.path.join(folder_path, subfolder)
        
        if not os.path.exists(folder_path):
            os.makedirs(folder_path, exist_ok=True)
        
        # Set up file watching if enabled
        watch_value = watch_for_changes == "True"
        if watch_value:
            self.setup_folder_watcher(folder_type, folder_path)
            status = f"Serving {folder_type} folder with change notifications: {folder_path}"
        else:
            status = f"Serving {folder_type} folder: {folder_path}"
        
        return (status,)
    
    def setup_folder_watcher(self, folder_type, folder_path):
        # Only set up watcher if not already watching this folder
        watcher_key = f"{folder_type}:{folder_path}"
        if watcher_key not in file_watchers:
            handler = FileChangeHandler(folder_type, folder_path)
            observer = Observer()
            observer.schedule(handler, folder_path, recursive=True)
            observer.start()
            file_watchers[watcher_key] = observer
            print(f"Started file watcher for {folder_type} folder: {folder_path}")

# # Register the node with ComfyUI
# NODE_CLASS_MAPPINGS = {
#     "FolderServerNode": FolderServerNode
# }

# # Register the node display name
# NODE_DISPLAY_NAME_MAPPINGS = {
#     "FolderServerNode": "Folder Server"
# }

# Add API routes
@server.PromptServer.instance.routes.get("/folder_server/list/{folder_type}")
async def list_folder(request):
    folder_type = request.match_info["folder_type"]
    
    if folder_type not in ["input", "output"]:
        return web.json_response({"error": "Invalid folder type"}, status=400)
    
    subfolder = request.query.get("subfolder", "")
    
    # Construct the folder path
    base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    folder_path = os.path.join(base_path, folder_type)
    
    if subfolder:
        folder_path = os.path.join(folder_path, subfolder)
    
    if not os.path.exists(folder_path):
        return web.json_response({"error": "Folder not found"}, status=404)
    
    files = []
    for file in os.listdir(folder_path):
        file_path = os.path.join(folder_path, file)
        file_info = {
            "name": file,
            "path": os.path.join(folder_type, subfolder, file) if subfolder else os.path.join(folder_type, file),
            "is_dir": os.path.isdir(file_path),
            "size": os.path.getsize(file_path) if not os.path.isdir(file_path) else 0,
            "modified": os.path.getmtime(file_path)
        }
        files.append(file_info)
    
    return web.json_response({"files": files})

@server.PromptServer.instance.routes.get("/folder_server/file/{folder_type}/{file_path:.*}")
async def get_file(request):
    folder_type = request.match_info["folder_type"]
    file_path = request.match_info["file_path"]
    
    if folder_type not in ["input", "output"]:
        return web.json_response({"error": "Invalid folder type"}, status=400)
    
    # Construct the full file path
    base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    full_path = os.path.join(base_path, folder_type, file_path)
    
    if not os.path.exists(full_path) or os.path.isdir(full_path):
        return web.json_response({"error": "File not found"}, status=404)
    
    # Create a response
    return web.FileResponse(full_path)

@server.PromptServer.instance.routes.post("/folder_server/upload/{folder_type}")
async def upload_file(request):
    folder_type = request.match_info["folder_type"]
    
    if folder_type not in ["input", "output"]:
        return web.json_response({"error": "Invalid folder type"}, status=400)
    
    # Get subfolder if provided
    data = await request.post()
    subfolder = data.get("subfolder", "")
    
    # Construct the folder path
    base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    folder_path = os.path.join(base_path, folder_type)
    
    if subfolder:
        folder_path = os.path.join(folder_path, subfolder)
        os.makedirs(folder_path, exist_ok=True)
    
    # Process uploaded file
    uploaded_file = data.get("file")
    if not uploaded_file:
        return web.json_response({"error": "No file uploaded"}, status=400)
    
    file_path = os.path.join(folder_path, uploaded_file.filename)
    
    # Save the file
    with open(file_path, "wb") as f:
        f.write(uploaded_file.file.read())
    
    return web.json_response({
        "success": True,
        "filename": uploaded_file.filename,
        "path": os.path.join(folder_type, subfolder, uploaded_file.filename) if subfolder else os.path.join(folder_type, uploaded_file.filename)
    })

@server.PromptServer.instance.routes.delete("/folder_server/file/{folder_type}/{file_path:.*}")
async def delete_file(request):
    folder_type = request.match_info["folder_type"]
    file_path = request.match_info["file_path"]
    
    if folder_type not in ["input", "output"]:
        return web.json_response({"error": "Invalid folder type"}, status=400)
    
    # Construct the full file path
    base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    full_path = os.path.join(base_path, folder_type, file_path)
    
    if not os.path.exists(full_path):
        return web.json_response({"error": "File not found"}, status=404)
    
    # Delete the file or directory
    if os.path.isdir(full_path):
        shutil.rmtree(full_path)
    else:
        os.remove(full_path)
    
    return web.json_response({"success": True})

# Add route to create directories
@server.PromptServer.instance.routes.post("/folder_server/create_dir/{folder_type}")
async def create_directory(request):
    folder_type = request.match_info["folder_type"]
    
    if folder_type not in ["input", "output"]:
        return web.json_response({"error": "Invalid folder type"}, status=400)
    
    # Get data from request
    data = await request.json()
    path = data.get("path", "")
    
    if not path:
        return web.json_response({"error": "Path is required"}, status=400)
    
    # Construct the full directory path
    base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    full_path = os.path.join(base_path, folder_type, path)
    
    # Create the directory
    try:
        os.makedirs(full_path, exist_ok=True)
        return web.json_response({"success": True, "path": os.path.join(folder_type, path)})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# Add route to move/rename files
@server.PromptServer.instance.routes.post("/folder_server/move")
async def move_file(request):
    data = await request.json()
    source_path = data.get("source", "")
    dest_path = data.get("destination", "")
    
    if not source_path or not dest_path:
        return web.json_response({"error": "Source and destination paths are required"}, status=400)
    
    # Construct the full paths
    base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    source_full_path = os.path.join(base_path, source_path)
    dest_full_path = os.path.join(base_path, dest_path)
    
    if not os.path.exists(source_full_path):
        return web.json_response({"error": "Source file not found"}, status=404)
    
    # Move the file or directory
    try:
        os.makedirs(os.path.dirname(dest_full_path), exist_ok=True)
        shutil.move(source_full_path, dest_full_path)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# Add route to copy files
@server.PromptServer.instance.routes.post("/folder_server/copy")
async def copy_file(request):
    data = await request.json()
    source_path = data.get("source", "")
    dest_path = data.get("destination", "")
    
    if not source_path or not dest_path:
        return web.json_response({"error": "Source and destination paths are required"}, status=400)
    
    # Construct the full paths
    base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    source_full_path = os.path.join(base_path, source_path)
    dest_full_path = os.path.join(base_path, dest_path)
    
    if not os.path.exists(source_full_path):
        return web.json_response({"error": "Source file not found"}, status=404)
    
    # Copy the file or directory
    try:
        os.makedirs(os.path.dirname(dest_full_path), exist_ok=True)
        if os.path.isdir(source_full_path):
            shutil.copytree(source_full_path, dest_full_path)
        else:
            shutil.copy2(source_full_path, dest_full_path)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

# WebSocket handler for file change notifications
@server.PromptServer.instance.routes.get("/ws/folder_server")
async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    # Store connection with empty subscriptions
    websocket_connections[ws] = set()
    
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    
                    # Handle subscription message
                    if data.get("action") == "subscribe":
                        folder_type = data.get("folder_type")
                        if folder_type in ["input", "output"]:
                            websocket_connections[ws].add(folder_type)
                            await ws.send_json({ 
                                "action": "subscribe_success",
                                "folder_type": folder_type
                            })
                    
                    # Handle unsubscribe message
                    elif data.get("action") == "unsubscribe":
                        folder_type = data.get("folder_type")
                        if folder_type in websocket_connections[ws]:
                            websocket_connections[ws].remove(folder_type)
                            await ws.send_json({
                                "action": "unsubscribe_success",
                                "folder_type": folder_type
                            })
                    
                except json.JSONDecodeError:
                    await ws.send_json({"error": "Invalid JSON format"})
            
            elif msg.type == web.WSMsgType.ERROR:
                print(f"WebSocket connection closed with exception {ws.exception()}")
    
    finally:
        # Clean up connection when client disconnects
        if ws in websocket_connections:
            del websocket_connections[ws]
    
    return ws

# Add route to manually check file events (useful for testing)
@server.PromptServer.instance.routes.post("/folder_server/check_files/{folder_type}")
async def check_files(request):
    folder_type = request.match_info["folder_type"]
    
    if folder_type not in ["input", "output"]:
        return web.json_response({"error": "Invalid folder type"}, status=400)
    
    # Get data from request
    data = await request.json()
    subfolder = data.get("subfolder", "")
    
    # Construct the folder path
    base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    folder_path = os.path.join(base_path, folder_type)
    
    if subfolder:
        folder_path = os.path.join(folder_path, subfolder)
    
    if not os.path.exists(folder_path):
        return web.json_response({"error": "Folder not found"}, status=404)
    
    # Trigger a manual check of the folder
    return web.json_response({
        "success": True,
        "message": f"Manual check requested for {folder_type}/{subfolder}",
        "folder_path": folder_path
    })

# Clean up file watchers when ComfyUI shuts down
def cleanup_watchers():
    for key, observer in file_watchers.items():
        observer.stop()
    
    # Wait for all observer threads to finish
    for key, observer in file_watchers.items():
        observer.join()
    
    print("All file watchers stopped")

# Register cleanup function to run when ComfyUI shuts down
import atexit
atexit.register(cleanup_watchers)

# Create a simple API documentation page with WebSocket information
@server.PromptServer.instance.routes.get("/folder_server/docs")
async def api_docs(request):
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Folder Server API Documentation</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; line-height: 1.6; }
            h1 { color: #333; }
            h2 { color: #555; margin-top: 30px; }
            h3 { color: #666; }
            pre { background: #f4f4f4; padding: 10px; border-radius: 5px; overflow-x: auto; }
            .endpoint { margin-bottom: 20px; }
            .method { font-weight: bold; }
            .url { color: #0066cc; }
            .test-section { margin-top: 40px; padding: 20px; background: #f9f9f9; border-radius: 5px; }
            .test-output { height: 200px; overflow-y: auto; background: #000; color: #0f0; padding: 10px; font-family: monospace; }
            button { padding: 8px 15px; background: #0066cc; color: white; border: none; border-radius: 4px; cursor: pointer; }
            button:hover { background: #0055aa; }
            select, input { padding: 8px; border-radius: 4px; border: 1px solid #ccc; }
        </style>
    </head>
    <body>
        <h1>Folder Server API Documentation</h1>
        
        <div class="endpoint">
            <h2>REST API Endpoints</h2>
            
            <h3>List Folder Contents</h3>
            <p><span class="method">GET</span> <span class="url">/folder_server/list/{folder_type}?subfolder={subfolder}</span></p>
            <p>Lists all files and directories in the specified folder.</p>
            <p>Parameters:</p>
            <ul>
                <li><strong>folder_type</strong>: Either "input" or "output"</li>
                <li><strong>subfolder</strong> (optional): Subfolder path</li>
            </ul>
        </div>
        
        <div class="endpoint">
            <h3>Get File</h3>
            <p><span class="method">GET</span> <span class="url">/folder_server/file/{folder_type}/{file_path}</span></p>
            <p>Downloads a file from the specified folder.</p>
        </div>
        
        <div class="endpoint">
            <h3>Upload File</h3>
            <p><span class="method">POST</span> <span class="url">/folder_server/upload/{folder_type}</span></p>
            <p>Uploads a file to the specified folder.</p>
            <p>Form data:</p>
            <ul>
                <li><strong>file</strong>: The file to upload</li>
                <li><strong>subfolder</strong> (optional): Subfolder path</li>
            </ul>
        </div>
        
        <div class="endpoint">
            <h3>Delete File</h3>
            <p><span class="method">DELETE</span> <span class="url">/folder_server/file/{folder_type}/{file_path}</span></p>
            <p>Deletes a file or directory from the specified folder.</p>
        </div>
        
        <div class="endpoint">
            <h3>Create Directory</h3>
            <p><span class="method">POST</span> <span class="url">/folder_server/create_dir/{folder_type}</span></p>
            <p>Creates a new directory in the specified folder.</p>
            <p>JSON body:</p>
            <pre>{"path": "path/to/new/directory"}</pre>
        </div>
        
        <div class="endpoint">
            <h3>Move/Rename File</h3>
            <p><span class="method">POST</span> <span class="url">/folder_server/move</span></p>
            <p>Moves or renames a file or directory.</p>
            <p>JSON body:</p>
            <pre>{"source": "input/path/to/file", "destination": "output/new/path/to/file"}</pre>
        </div>
        
        <div class="endpoint">
            <h3>Copy File</h3>
            <p><span class="method">POST</span> <span class="url">/folder_server/copy</span></p>
            <p>Copies a file or directory.</p>
            <p>JSON body:</p>
            <pre>{"source": "input/path/to/file", "destination": "output/new/path/to/file"}</pre>
        </div>
        
        <h2>WebSocket API for File Change Notifications</h2>
        <p>Connect to the WebSocket endpoint to receive real-time file change notifications:</p>
        <p><span class="url">ws://localhost:8188/ws/folder_server</span></p>
        
        <h3>WebSocket Messages</h3>
        
        <h4>Subscribe to a folder:</h4>
        <pre>{"action": "subscribe", "folder_type": "input"}</pre>
        
        <h4>Unsubscribe from a folder:</h4>
        <pre>{"action": "unsubscribe", "folder_type": "input"}</pre>
        
        <h4>Event notifications you will receive:</h4>
        <ul>
            <li><strong>file_created</strong>: When a new file is created</li>
            <li><strong>file_modified</strong>: When a file is modified</li>
            <li><strong>file_deleted</strong>: When a file is deleted</li>
            <li><strong>file_moved</strong>: When a file is moved or renamed</li>
            <li><strong>directory_created</strong>: When a new directory is created</li>
            <li><strong>directory_deleted</strong>: When a directory is deleted</li>
            <li><strong>directory_moved</strong>: When a directory is moved or renamed</li>
        </ul>
        
        <div class="test-section">
            <h2>WebSocket Test Console</h2>
            <div>
                <select id="folderType">
                    <option value="input">input</option>
                    <option value="output">output</option>
                </select>
                <button id="connectBtn">Connect</button>
                <button id="disconnectBtn" disabled>Disconnect</button>
            </div>
            <div style="margin-top: 10px;">
                <button id="subscribeBtn" disabled>Subscribe</button>
                <button id="unsubscribeBtn" disabled>Unsubscribe</button>
            </div>
            <h3>Event Log:</h3>
            <div id="output" class="test-output"></div>
            
            <script>
                let ws = null;
                let selectedFolder = document.getElementById('folderType').value;
                
                document.getElementById('folderType').addEventListener('change', (e) => {
                    selectedFolder = e.target.value;
                });
                
                function appendToOutput(message, isError = false) {
                    const output = document.getElementById('output');
                    const msgElement = document.createElement('div');
                    msgElement.textContent = message;
                    if (isError) {
                        msgElement.style.color = '#ff6b6b';
                    }
                    output.appendChild(msgElement);
                    output.scrollTop = output.scrollHeight;
                }
                
                document.getElementById('connectBtn').addEventListener('click', () => {
                    if (ws) {
                        appendToOutput('Already connected', true);
                        return;
                    }
                    
                    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
                    const wsUrl = `${protocol}//${window.location.host}/ws/folder_server`;
                    
                    appendToOutput(`Connecting to ${wsUrl}...`);
                    ws = new WebSocket(wsUrl);
                    
                    ws.onopen = () => {
                        appendToOutput('Connected!');
                        document.getElementById('connectBtn').disabled = true;
                        document.getElementById('disconnectBtn').disabled = false;
                        document.getElementById('subscribeBtn').disabled = false;
                        document.getElementById('unsubscribeBtn').disabled = false;
                    };
                    
                    ws.onmessage = (event) => {
                        const data = JSON.parse(event.data);
                        appendToOutput('Received: ' + JSON.stringify(data, null, 2));
                    };
                    
                    ws.onclose = () => {
                        appendToOutput('Disconnected');
                        ws = null;
                        document.getElementById('connectBtn').disabled = false;
                        document.getElementById('disconnectBtn').disabled = true;
                        document.getElementById('subscribeBtn').disabled = true;
                        document.getElementById('unsubscribeBtn').disabled = true;
                    };
                    
                    ws.onerror = (error) => {
                        appendToOutput('Error: ' + error.message, true);
                    };
                });
                
                document.getElementById('disconnectBtn').addEventListener('click', () => {
                    if (ws) {
                        ws.close();
                    }
                });
                
                document.getElementById('subscribeBtn').addEventListener('click', () => {
                    if (ws && ws.readyState === WebSocket.OPEN) {
                        const message = {
                            action: 'subscribe',
                            folder_type: selectedFolder
                        };
                        ws.send(JSON.stringify(message));
                        appendToOutput('Sent: ' + JSON.stringify(message));
                    } else {
                        appendToOutput('WebSocket not connected', true);
                    }
                });
                
                document.getElementById('unsubscribeBtn').addEventListener('click', () => {
                    if (ws && ws.readyState === WebSocket.OPEN) {
                        const message = {
                            action: 'unsubscribe',
                            folder_type: selectedFolder
                        };
                        ws.send(JSON.stringify(message));
                        appendToOutput('Sent: ' + JSON.stringify(message));
                    } else {
                        appendToOutput('WebSocket not connected', true);
                    }
                });
            </script>
        </div>
    </body>
    </html>
    """
    return web.Response(text=html, content_type="text/html")

print("Folder Server node with file change notifications loaded!")

# Add this at the end of your file (folder_server.py)

# Initialize default watchers for input and output folders when ComfyUI starts
def initialize_default_watchers():
    base_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    
    # Set up watchers for main input and output folders
    input_folder = os.path.join(base_path, "input")
    output_folder = os.path.join(base_path, "output")
    
    # Create folders if they don't exist
    os.makedirs(input_folder, exist_ok=True)
    os.makedirs(output_folder, exist_ok=True)
    
    # Set up watchers
    input_handler = FileChangeHandler("input", input_folder)
    input_observer = Observer()
    input_observer.schedule(input_handler, input_folder, recursive=True)
    input_observer.start()
    file_watchers["input:default"] = input_observer
    
    output_handler = FileChangeHandler("output", output_folder)
    output_observer = Observer()
    output_observer.schedule(output_handler, output_folder, recursive=True)
    output_observer.start()
    file_watchers["output:default"] = output_observer
    
    print("Folder Server: Automatically started file watchers for input and output folders")

# Call this function when the module is imported
initialize_default_watchers()
