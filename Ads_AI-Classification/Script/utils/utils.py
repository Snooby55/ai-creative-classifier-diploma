
import os
import base64
import numpy as np
from io import BytesIO
import cv2
from PIL import Image
import imagehash
import time

from Script.core.console_bus import emit_log

def recursive_call(func, delay, *args, max_retries: int = 5, **kwargs):
    for attempt in range(1, max_retries + 1):
        result = func(*args, **kwargs)
        if result:
            return result
        emit_log(f'Recursive attempt {attempt} failed; retrying after {delay}s')
        time.sleep(delay)
    emit_log('Max retries exceeded')
    return False

def image_to_base64(img, image_format="JPEG"):
    buffered = BytesIO()
    img.save(buffered, format=image_format)
    encoded = base64.b64encode(buffered.getvalue()).decode('utf-8')
    return encoded

def merge_images_horizontally(image1_path, image2_path, save_path):
    img1 = Image.open(image1_path)
    img2 = Image.open(image2_path)

    if img1.height != img2.height:
        new_height = min(img1.height, img2.height)
        img1 = img1.resize((int(img1.width * new_height / img1.height), new_height))
        img2 = img2.resize((int(img2.width * new_height / img2.height), new_height))

    merged_width = img1.width + img2.width
    merged_img = Image.new('RGB', (merged_width, img1.height))
    merged_img.paste(img1, (0, 0))
    merged_img.paste(img2, (img1.width, 0))
    merged_img.save(save_path)

def extract_campaign_name(filename):
    try:
        parts = filename.split('_')
        if len(parts) >= 2:
            return parts[1]  # Назва кампанії
    except Exception:
        return None

def extract_gif_frame(image_path, frame_ratio=0.5):
    try:
        with Image.open(image_path) as img:
            if not getattr(img, "is_animated", False):
                return img.convert("RGB")
            total_frames = img.n_frames
            target_frame = int(frame_ratio * (total_frames - 1))
            img.seek(target_frame)
            return img.convert("RGB")
    except Exception as e:
        emit_log(f"Помилка обробки GIF: {e}")
        return None

def compute_phash_from_image(img):
    try:
        img = img.convert('L').resize((64, 64))
        return imagehash.phash(img)
    except Exception:
        return None

def compute_phash_from_path(image_path, frame_ratio=0.5):
    try:
        if image_path.lower().endswith('.gif'):
            img = extract_gif_frame(image_path, frame_ratio)
        else:
            img = Image.open(image_path)
        if img is None:
            return None
        return compute_phash_from_image(img)
    except Exception:
        return None


def extract_video_frames(video_path, number_of_frames=3, start_pct=0.3, end_pct=0.7):
    try:
        cap = cv2.VideoCapture(video_path)
        if not cap.isOpened():
            return None

        total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if total_frames <= 0:
            cap.release()
            return None

        start_frame = int(start_pct * (total_frames - 1))
        end_frame = int(end_pct * (total_frames - 1))

        if end_frame < start_frame:
            cap.release()
            return None

        frame_indices = np.linspace(start_frame, end_frame, min(number_of_frames, end_frame - start_frame + 1), dtype=int)
        frames = []

        for frame_idx in frame_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ret, frame = cap.read()
            if not ret:
                continue

            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append(Image.fromarray(frame_rgb))

        cap.release()
        return frames if frames else None
    except Exception as e:
        emit_log(f"Помилка обробки відео {video_path}: {e}")
        return None

def merge_similar_pairs(similar_pairs, campaign_files):
    graph = {}
    for file1, file2, sim, method in similar_pairs:
        if sim > 0:
            graph.setdefault(file1, set()).add(file2)
            graph.setdefault(file2, set()).add(file1)

    for f in campaign_files:
        graph.setdefault(f, set())

    visited = set()
    groups = []
    for node in graph:
        if node not in visited:
            stack = [node]
            component = set()
            while stack:
                current = stack.pop()
                if current not in visited:
                    visited.add(current)
                    component.add(current)
                    stack.extend(graph[current] - visited)
            groups.append(component)

    result = {}
    for group in groups:
        rep = sorted(group)[0]
        duplicates = group - {rep}
        result[rep] = list(duplicates) if duplicates else None

    return result

# Функції для порівняння зображень та відео

def compare_images_hash(file1, file2, processor, tolerance=0):
    phash1 = processor.image_phashes.get(file1)
    phash2 = processor.image_phashes.get(file2)
    if phash1 is None or phash2 is None:
        return 0.0
    d = abs(phash1 - phash2)
    return 1.0 if d <= tolerance else 0.0

def compare_images_clip(file1, file2, processor):
    try:
        idx1 = processor.image_files.index(file1)
        idx2 = processor.image_files.index(file2)
        
        from sentence_transformers import util
        emb1 = processor.embeddings[idx1]
        emb2 = processor.embeddings[idx2]
        sim = util.cos_sim(emb1, emb2).item()
        return sim
    except Exception:
        return 0.0

def compare_video_hash(file1, file2, processor, tolerance=0):
    phashes1 = processor.video_phashes.get(file1)
    phashes2 = processor.video_phashes.get(file2)

    if not phashes1 or not phashes2:
        return 0.0

    for h1 in phashes1:
        for h2 in phashes2:
            d = abs(h1 - h2)
            if d <= tolerance:
                return 1.0
    return 0.0


# Функція для дебага
def merge_similar_images_from_dict(result_dict, output_dir='merged_images'):
    os.makedirs(output_dir, exist_ok=True)
    for rep, duplicates in result_dict.items():
        if duplicates is not None:
            for duplicate in duplicates:
                merged_filename = f"{os.path.splitext(os.path.basename(rep))[0]}___{os.path.splitext(os.path.basename(duplicate))[0]}.jpg"
                save_path = os.path.join(output_dir, merged_filename)
                merge_images_horizontally(rep, duplicate, save_path)
                emit_log(f"Мерджено {os.path.basename(rep)} та {os.path.basename(duplicate)} -> {merged_filename}")
