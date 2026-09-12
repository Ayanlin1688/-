"""Explicitly synthetic screenshot data, never loaded by the application."""
from pathlib import Path
from PyQt5.QtGui import QColor, QFont, QImage, QPainter
from PyQt5.QtCore import Qt, QRect
from core.config_manager import ConfigManager
from core.matcher import StoryboardMatcher
from core.prompt_detector import annotate_tasks
import time


def make_fixture(root):
    root = Path(root)
    for name in ('提示词', '参考图片', '视频输出'):
        (root / name).mkdir()
    names = ['柔软纹理', '铺床展示', '沙发盖毯', '卧室氛围', '桌面送风', '车内降温', '风扇特写', '便携收纳']
    colors = ['#5e6ad2', '#157f80', '#b06c28']
    for n, name in enumerate(names):
        product = '玫瑰毯子' if n < 4 else '车载风扇'
        prompt_dir, image_dir = root / '提示词' / product, root / '参考图片' / product
        prompt_dir.mkdir(exist_ok=True); image_dir.mkdir(exist_ok=True)
        h3_prompt = (f'subject_definitions:\n<Subject 1> is {name}，外观参考 <Picture 1>。\n\n'
                     f'summary:\n[reference generation] 展示{name}。\n\n'
                     'retention_analysis:\n<Subject 1>: fully_preserved\n\n'
                     'detailed_description:\n[Shot 1] 自然光下展示 <Picture 1>，保持产品完整形态。\n'
                     '[Shot 2] At 00:03.000, cut to <Picture 2> 和 <Picture 3> 的细节。\n\n'
                     'overall_soundscape:\n自然环境音。\n\nnon_diegetic_music:\nN/A')
        prompt = [h3_prompt,
                  '镜头1：展示@图1。镜头2：结合@图2、@图3展示细节。',
                  '自然光下缓缓展示产品，镜头靠近表面纹理。'][n % 3]
        (prompt_dir / f'{n%4+1:02d}_{name}.txt').write_text(prompt, encoding='utf-8')
        for k in range(3):
            image = QImage(320, 220, QImage.Format_RGB32); image.fill(QColor('#141416'))
            painter = QPainter(image); painter.setRenderHint(QPainter.Antialiasing)
            painter.setBrush(QColor(colors[k % 3])); painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(20, 20, 280, 180, 24, 24)
            painter.setPen(QColor('#ffffff')); painter.setFont(QFont('Microsoft YaHei', 24))
            painter.drawText(QRect(20, 45, 280, 110), Qt.AlignCenter, f'参考图 {k+1}\n{name}')
            painter.setFont(QFont('Microsoft YaHei', 11))
            painter.drawText(QRect(20, 165, 280, 25), Qt.AlignCenter, '仅用于界面截图的本地素材')
            painter.end()
            image.save(str(image_dir / f'{n%4+1}({k+1}).png'))
    manager = ConfigManager(root / 'config.json'); manager.load_config()
    manager.config['paths'] = dict(prompts=str(root / '提示词'), images=str(root / '参考图片'), output=str(root / '视频输出'))
    manager.config['workspace'].update(model='MiniMax-H3', aspect_ratio='9:16', resolution='1080p', duration=8)
    manager.config['defaults'].update(model='MiniMax-H3', aspect_ratio='9:16', resolution='1080p', duration=8)
    manager.config['model_pool'].update(enabled=True, strategy='轮询', cooldown=30, models=[
        dict(name='video-v2', enabled=True, status='冷却中', cooldown_until=time.time()+30),
        dict(name='video-v3', enabled=True, status='健康'),
        dict(name='seedance-2.5', enabled=True, status='健康')])
    manager.config['task_strategy'].update(max_concurrency=2, max_retries=5)
    tasks = StoryboardMatcher.from_config(manager.config).scan_and_match(manager.config['paths'])
    annotate_tasks(tasks, manager.config)
    tasks[2].update(model_source='manual', requested_model='video-v3', model='video-v3', model_locked=True)
    manager.config['model_overrides'][tasks[2]['prompt_path']] = 'video-v3'
    states = ['completed', 'completed', 'submission_unknown', 'duplicate', 'processing', 'retry_wait', 'waiting', 'waiting']
    for index, task in enumerate(tasks):
        task.update(local_id=f'screenshot-{index}', status=states[index], model=task['requested_model'],
                    task_id=f'local_fixture_{index+1:03d}' if states[index] not in {'waiting', 'submission_unknown'} else '',
                    created_at='2026-09-12T14:00:00', finished_at='2026-09-12T14:02:15' if states[index] == 'completed' else '',
                    result_path=str(root / '视频输出' / task['product'] / f'{task["product_task_index"]:03d}_{task["prompt_name"]}.mp4') if states[index] == 'completed' else '',
                    size_bytes=8600000 if states[index] == 'completed' else 0, error='本地截图示例')
    manager.config['schedule'].update(enabled=True, time='23:00', mode='daily')
    tasks[2]['error'] = 'HTTP 429，提交结果未确认；已阻止重新创建，请核对服务商后台。'
    manager.config['history'] = [t for t in tasks if t['status'] != 'waiting']
    manager.save_config()
    return manager, tasks
