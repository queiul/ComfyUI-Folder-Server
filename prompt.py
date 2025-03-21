import os
import json
import folder_paths
from PIL import Image
from aiohttp import web
import logging
import traceback
from server import PromptServer
import subprocess
import shutil

# Get the PromptServer instance
prompt_server = PromptServer.instance

# Dictionary to store custom metadata for each prompt_id
custom_metadata_store = {}

# Custom routes for our API extension
custom_routes = web.RouteTableDef()

# Check if PngInfo is available, otherwise create a compatible implementation
try:
    from PIL import PngInfo
except ImportError:
    # Create a simple PngInfo replacement class for older PIL versions
    class PngInfo:
        def __init__(self):
            self.text = {}
            
        def add_text(self, key, value):
            self.text[key] = value

@custom_routes.post("/folder_server/prompt")
async def custom_prompt(request):
    """Custom endpoint to handle prompts with additional metadata"""
    try:
        # Parse the incoming JSON data
        json_data = await request.json()
        
        # Extract custom metadata if present
        custom_metadata = json_data.pop("custom_metadata", {})
        
        if not custom_metadata:
            return web.json_response({"error": "No custom_metadata provided"}, status=400)
        
        # Forward the request to the original prompt handler
        # But first get the original handler
        original_handler = None
        for route in prompt_server.routes:
            if route.method == "POST" and route.path == "/prompt":
                original_handler = route.handler
                break
        
        if original_handler is None:
            return web.json_response({"error": "Failed to find original prompt handler"}, status=500)
        
        # Call the original handler
        response = await original_handler(request)
        
        # Extract the response data
        if response.status == 200:
            response_text = await response.text()
            response_data = json.loads(response_text)
            prompt_id = response_data.get("prompt_id")
            
            # Store the custom metadata with the prompt_id for later use
            if prompt_id:
                custom_metadata_store[prompt_id] = custom_metadata
                # Add an indicator that we've stored metadata
                response_data["custom_metadata_status"] = "stored"
                return web.json_response(response_data)
        
        # If something went wrong, return the original response
        return response
        
    except Exception as e:
        logging.error(f"Error in custom_prompt: {e}")
        logging.error(traceback.format_exc())
        return web.json_response({"error": str(e)}, status=500)

# Add our routes to the server
prompt_server.app.add_routes(custom_routes)

def inject_metadata_to_image(file_path, metadata):
    """
    Adds custom metadata to an image file.
    Supports multiple image formats based on extension.
    
    Args:
        file_path: Path to the image file
        metadata: Dictionary containing metadata to add
    """
    try:
        if not os.path.exists(file_path):
            logging.warning(f"File not found: {file_path}")
            return False
            
        file_ext = os.path.splitext(file_path)[1].lower()
        
        # Handle PNG files
        if file_ext == ".png":
            try:
                # Try the newer PIL approach with PngInfo
                img = Image.open(file_path)
                
                # Convert metadata to JSON string
                metadata_str = json.dumps(metadata)
                
                # Method 1: Using PngInfo if available
                try:
                    # Create PNG info object
                    png_info = PngInfo()
                    
                    # Add existing metadata if present
                    if hasattr(img, 'text'):
                        for key, value in img.text.items():
                            png_info.add_text(key, value)
                    
                    # Add our custom metadata as a JSON string
                    png_info.add_text("custom_metadata", metadata_str)
                    
                    # Save the image with new metadata
                    img.save(file_path, pnginfo=png_info)
                    logging.info(f"Added custom metadata to PNG (using PngInfo): {file_path}")
                    return True
                    
                except Exception as pnginfo_error:
                    # Method 2: Fallback for older Pillow versions
                    metadata_dict = {}
                    
                    # Add existing metadata if present
                    if hasattr(img, 'info'):
                        metadata_dict.update(img.info)
                    
                    # Add our custom metadata
                    metadata_dict["custom_metadata"] = metadata_str
                    
                    # Save with metadata in info dictionary
                    img.save(file_path, **metadata_dict)
                    logging.info(f"Added custom metadata to PNG (using info dict): {file_path}")
                    return True
                    
            except Exception as png_error:
                logging.error(f"Error adding metadata to PNG: {png_error}")
                # Fall back to companion file
                metadata_file = f"{file_path}.metadata.json"
                with open(metadata_file, 'w') as f:
                    json.dump(metadata, f, indent=2)
                logging.info(f"Created companion metadata file for PNG: {metadata_file}")
                return True
            
        # Handle JPEG, TIFF, and other PIL-supported formats
        elif file_ext in [".jpg", ".jpeg", ".tiff", ".tif", ".webp", ".bmp"]:
            img = Image.open(file_path)
            
            # Create a temporary metadata file path
            metadata_file = f"{file_path}.metadata.json"
            
            # Write metadata to a companion file
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)
            
            # Try to update EXIF data if possible
            try:
                # For newer PIL versions
                if hasattr(img, 'getexif'):
                    exif_dict = img.getexif()
                    if exif_dict is not None:
                        # Use UserComment tag (0x9286) for a metadata reference
                        exif_dict[0x9286] = f"Metadata in companion file: {os.path.basename(metadata_file)}"
                        img.save(file_path, exif=exif_dict)
                # For older PIL versions
                elif hasattr(img, '_getexif'):
                    exif_dict = img._getexif() or {}
                    exif_dict[0x9286] = f"Metadata in companion file: {os.path.basename(metadata_file)}"
                    img.save(file_path, exif=exif_dict)
            except Exception as e:
                logging.warning(f"Could not add EXIF reference to {file_path}: {e}")
            
            logging.info(f"Added metadata to companion file for: {file_path}")
            return True
            
        # Handle video files using ffmpeg if available
        elif file_ext in [".mp4", ".mov", ".avi", ".webm", ".mkv"]:
            return inject_metadata_to_video(file_path, metadata)
            
        # Handle 3D model files
        elif file_ext in [".obj", ".stl", ".fbx", ".glb", ".gltf"]:
            return inject_metadata_to_3d_model(file_path, metadata)
            
        else:
            # For unsupported file types, create a companion metadata file
            metadata_file = f"{file_path}.metadata.json"
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)
            logging.info(f"Created companion metadata file for unsupported type {file_ext}: {metadata_file}")
            return True
            
    except Exception as e:
        logging.error(f"Error adding metadata to file {file_path}: {e}")
        logging.error(traceback.format_exc())
        
        # Last resort fallback - always create a companion file
        try:
            metadata_file = f"{file_path}.metadata.json"
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)
            logging.info(f"Created fallback companion metadata file: {metadata_file}")
            return True
        except:
            return False

def inject_metadata_to_video(video_path, metadata):
    """
    Adds custom metadata to a video file using MoviePy.
    
    Args:
        video_path: Path to the video file
        metadata: Dictionary containing metadata to add
    """
    try:
        # Check if moviepy is available
        try:
            import moviepy.editor as mp
        except ImportError:
            logging.warning("moviepy not found, creating companion metadata file instead")
            metadata_file = f"{video_path}.metadata.json"
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)
            logging.info(f"Created companion metadata file for video: {metadata_file}")
            return True
            
        # Load the video
        video = mp.VideoFileClip(video_path)
        
        # Convert metadata to a JSON string
        metadata_str = json.dumps(metadata)
        
        # Create a temp output file
        temp_output = f"{video_path}.temp{os.path.splitext(video_path)[1]}"
        
        # Here's where it gets tricky - MoviePy doesn't directly support 
        # adding metadata to videos. We have a few options:
        
        # Option 1: Use MoviePy to handle the video and then still use ffmpeg for metadata
        # This could actually be more elegant as we'd avoid needing to re-encode
        video.write_videofile(temp_output, codec='libx264')
        
        # Then use ffmpeg just for metadata (if available)
        try:
            subprocess.run(["ffmpeg", "-i", temp_output, "-c", "copy", "-map_metadata", "0"] + 
                          [item for k, v in metadata.items() for item in ["-metadata", f"{k}={v}"]] +
                          [video_path], check=True, capture_output=True)
        except (subprocess.SubprocessError, FileNotFoundError):
            # If ffmpeg still isn't available, just keep the video and add a companion file
            shutil.move(temp_output, video_path)
            metadata_file = f"{video_path}.metadata.json"
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)
            logging.info(f"Created companion metadata file for video: {metadata_file}")
        
        # Clean up the temp file if it still exists
        if os.path.exists(temp_output):
            os.remove(temp_output)
            
        return True
        
    except Exception as e:
        logging.error(f"Error adding metadata to video {video_path}: {e}")
        logging.error(traceback.format_exc())
        
        # Fallback to companion file
        try:
            metadata_file = f"{video_path}.metadata.json"
            with open(metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)
            logging.info(f"Created companion metadata file for video: {metadata_file}")
            return True
        except:
            return False

# Update the check_dependencies function
def check_dependencies():
    dependencies = {
        "ffmpeg": False,
        "moviepy": False,
    }
    
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        dependencies["ffmpeg"] = True
    except (subprocess.SubprocessError, FileNotFoundError):
        pass
        
    try:
        import moviepy
        dependencies["moviepy"] = True
    except ImportError:
        pass
        
    for dep, available in dependencies.items():
        if available:
            logging.info(f"Optional dependency {dep} is available")
        else:
            logging.warning(f"Optional dependency {dep} not found - some features will be limited")
            
    return dependencies

def inject_metadata_to_3d_model(model_path, metadata):
    """
    Adds custom metadata to a 3D model file.
    Creates a companion JSON file for most formats.
    For GLTF, attempts to embed metadata directly.
    
    Args:
        model_path: Path to the 3D model file
        metadata: Dictionary containing metadata to add
    """
    try:
        file_ext = os.path.splitext(model_path)[1].lower()
        
        # For GLTF JSON files, we can add metadata directly
        if file_ext == ".gltf":
            try:
                with open(model_path, 'r') as f:
                    model_data = json.load(f)
                
                # Add metadata to the extras field
                if "asset" not in model_data:
                    model_data["asset"] = {}
                if "extras" not in model_data["asset"]:
                    model_data["asset"]["extras"] = {}
                    
                model_data["asset"]["extras"]["custom_metadata"] = metadata
                
                # Write back to file
                with open(model_path, 'w') as f:
                    json.dump(model_data, f, indent=2)
                    
                logging.info(f"Added metadata directly to GLTF file: {model_path}")
                return True
            except Exception as e:
                logging.error(f"Error adding metadata to GLTF: {e}")
                # Fall back to companion file
        
        # For all other 3D formats, create a companion file
        metadata_file = f"{model_path}.metadata.json"
        with open(metadata_file, 'w') as f:
            json.dump(metadata, f, indent=2)
        logging.info(f"Created companion metadata file for 3D model: {metadata_file}")
        return True
        
    except Exception as e:
        logging.error(f"Error adding metadata to 3D model {model_path}: {e}")
        logging.error(traceback.format_exc())
        return False

# Hook into the execution finish handler
def execution_complete_handler(prompt_id, outputs):
    """
    Hook called when a prompt execution completes
    Args:
        prompt_id: ID of the prompt that was executed
        outputs: Output nodes and their data
    """
    try:
        # Check if we have custom metadata for this prompt_id
        if prompt_id not in custom_metadata_store:
            return
            
        metadata = custom_metadata_store[prompt_id]
        
        # Look for outputs in the results
        for node_id, node_output in outputs.items():
            for output_data in node_output["outputs"]:
                # Check if this output has a filename
                if "filename" in output_data and "type" in output_data:
                    filename = output_data["filename"]
                    output_type = output_data.get("type", "output")
                    subfolder = output_data.get("subfolder", "")
                    
                    # Get the output directory based on type
                    if output_type == "output":
                        output_dir = folder_paths.get_output_directory()
                    elif output_type == "temp":
                        output_dir = folder_paths.get_temp_directory()
                    elif output_type == "input":
                        output_dir = folder_paths.get_input_directory()
                    else:
                        continue
                        
                    # Build the full path to the file
                    if subfolder:
                        file_path = os.path.join(output_dir, subfolder, filename)
                    else:
                        file_path = os.path.join(output_dir, filename)
                    
                    # Add file-specific info to metadata
                    file_metadata = metadata.copy()
                    file_metadata["node_id"] = node_id
                    file_metadata["filename"] = filename
                    
                    # Inject the metadata based on file type
                    inject_metadata_to_image(file_path, file_metadata)
        
        # Clean up the metadata store
        del custom_metadata_store[prompt_id]
        
    except Exception as e:
        logging.error(f"Error in execution complete handler: {e}")
        logging.error(traceback.format_exc())

# Register our execution complete handler with ComfyUI
class ExecutionHooks:
    @classmethod
    def register(cls):
        try:
            # Ensure we can access execution
            import execution
            
            # Store the original function so we can call it
            original_func = execution.PromptExecutor.execute
            
            def execute_wrapper(self, prompt, prompt_id, extra_data={}, execute_outputs=[]):
                # Call the original function
                result = original_func(self, prompt, prompt_id, extra_data, execute_outputs)
                
                # After execution is complete, process the results
                if hasattr(self, 'history_result') and prompt_id in custom_metadata_store:
                    # Call our handler with the outputs
                    execution_complete_handler(prompt_id, self.history_result["outputs"])
                
                return result
            
            # Replace the function with our wrapper
            execution.PromptExecutor.execute = execute_wrapper
            logging.info("Successfully registered execution hooks for custom metadata")
                        
        except Exception as e:
            logging.error(f"Failed to register execution hooks: {e}")
            logging.error(traceback.format_exc())


# Register our hooks when this file is loaded
ExecutionHooks.register()

# Check dependencies
available_deps = check_dependencies()

# Print a message to show the extension is loaded
logging.info("Enhanced Metadata API Extension loaded - use /folder_server/prompt endpoint to add metadata to various file types")
