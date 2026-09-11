"""Explicitly synthetic screenshot data, never loaded by the application."""
from pathlib import Path
from PyQt5.QtGui import QColor, QFont, QImage, QPainter
from PyQt5.QtCore import Qt, QRect
from core.config_manager import ConfigManager


def make_fixture(root):
    root = Path(root)
    for name in ('提示词', '参考图片', '视频输出'):
        (root / name).mkdir()
    names = ['汽车外观', '车轮特写', '夜景行驶', '内饰细节', '山路转弯', '晨光出发', '雨中街景', '海边停车']
    colors = ['#5e6ad2', '#157f80', '#b06c28']
    for n, name in enumerate(names):
        (root / '提示词' / f'{n+1:02d}_{name}.txt').write_text('<Picture 1> 镜头缓慢环绕汽车', encoding='utf-8')
        for k in range(3 if n == 0 else 1):
            image = QImage(320, 220, QImage.Format_RGB32); image.fill(QColor('#141416'))
            painter = QPainter(image); painter.setRenderHint(QPainter.Antialiasing)
            painter.setBrush(QColor(colors[k % 3])); painter.setPen(Qt.NoPen)
            painter.drawRoundedRect(20, 20, 280, 180, 24, 24)
            painter.setPen(QColor('#ffffff')); painter.setFont(QFont('Microsoft YaHei', 24))
            painter.drawText(QRect(20, 45, 280, 110), Qt.AlignCenter, f'参考图 {k+1}\n{name}')
            painter.setFont(QFont('Microsoft YaHei', 11))
            painter.drawText(QRect(20, 165, 280, 25), Qt.AlignCenter, '仅用于界面截图的本地素材')
            painter.end()
            image.save(str(root / '参考图片' / f'{n+1:02d}_{name}_{k+1}.png'))
    manager = ConfigManager(root / 'config.json'); manager.load_config()
    manager.config['paths'] = dict(prompts=str(root / '提示词'), images=str(root / '参考图片'), output=str(root / '视频输出'))
    manager.config['workspace'].update(model='MiniMax-H3', aspect_ratio='9:16', resolution='1080p', duration=8)
    manager.config['defaults'].update(model='MiniMax-H3', aspect_ratio='9:16', resolution='1080p', duration=8)
    tasks = []
    states = ['completed', 'processing', 'waiting', 'failed', 'completed', 'waiting', 'skipped', 'waiting']
    for index, name in enumerate(names):
        tasks.append(dict(local_id=f'screenshot-{index}', prompt_name=name, prompt_path=str(root / '提示词' / f'{index+1:02d}_{name}.txt'),
                          images=[str(p) for p in sorted((root / '参考图片').glob(f'{index+1:02d}_*.png'))], matched=True,
                          status=states[index], model='MiniMax-H3', task_id=f'local_fixture_{index+1:03d}' if states[index] != 'waiting' else '',
                          created_at='2026-09-10T14:00:00', finished_at='2026-09-10T14:02:15' if states[index] == 'completed' else '',
                          result_path=str(root / '视频输出' / f'{index+1:03d}_{name}.mp4') if states[index] == 'completed' else '',
                          size_bytes=8600000 if states[index] == 'completed' else 0, error='本地截图示例'))
    manager.config['history'] = [t for t in tasks if t['status'] != 'waiting']
    manager.save_config()
    return manager, tasks
