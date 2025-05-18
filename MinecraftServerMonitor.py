#!/usr/bin/env python3
import sqlite3
import time
import argparse
from datetime import datetime, timedelta
from mcstatus import JavaServer
import matplotlib.pyplot as plt
from statistics import mean, stdev
import textwrap
import threading
import queue
import sys
import msvcrt

plt.rcParams['font.sans-serif'] = ['SimHei']  # 修复中文显示
plt.rcParams['axes.unicode_minus'] = False
plt.switch_backend('Agg')  # 非交互式后端

# ==================== 配置区 ====================
CONFIG = {
    "interval": 300,  # 数据采集间隔（秒）
    "db_name": "server_stats.db",
    "never_delete_data": True
}
# ================================================

class DBAccess:
    def __init__(self):
        self.conn = sqlite3.connect(CONFIG['db_name'], check_same_thread=False)
        self.lock = threading.Lock()
        self._init_db()
        
    def _init_db(self):
        with self.lock:
            c = self.conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS servers
                        (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT UNIQUE,
                        ip TEXT UNIQUE)''')
            c.execute('''CREATE TABLE IF NOT EXISTS stats
                        (id INTEGER PRIMARY KEY AUTOINCREMENT,
                        server_id INTEGER,
                        online INTEGER,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(server_id) REFERENCES servers(id) ON DELETE CASCADE)''')
            self.conn.commit()

    def execute(self, query, args=()):
        with self.lock:
            c = self.conn.cursor()
            c.execute(query, args)
            self.conn.commit()
            return c

class WindowsInput:
    def __init__(self):
        self.buffer = []
        self.input_thread = threading.Thread(target=self._input_loop, daemon=True)
        self.input_thread.start()
        self.cmd_queue = queue.Queue()

    def _input_loop(self):
        while True:
            if msvcrt.kbhit():
                char = msvcrt.getwch()
                if char == '\r':  # 回车键
                    cmd = ''.join(self.buffer).strip().lower()
                    if cmd:
                        self.cmd_queue.put(cmd)
                    self.buffer.clear()
                    print()
                elif char == '\x08':  # 退格键
                    if self.buffer:
                        self.buffer.pop()
                        sys.stdout.write('\b \b')
                else:
                    self.buffer.append(char)
                    sys.stdout.write(char)
                sys.stdout.flush()

    def get_cmd(self):
        try:
            return self.cmd_queue.get_nowait()
        except queue.Empty:
            return None

class MCMonitor:
    def __init__(self, never_delete=False):
        self.db = DBAccess()
        CONFIG['never_delete_data'] = never_delete
        self.running = True
        self.input_handler = WindowsInput()

    def load_servers(self):
        c = self.db.execute("SELECT name, ip FROM servers")
        return [{"name": row[0], "ip": row[1]} for row in c.fetchall()]

    def _get_server_id(self, name):
        c = self.db.execute("SELECT id FROM servers WHERE name = ?", (name,))
        result = c.fetchone()
        return result[0] if result else None

    def collect_data(self):
        servers = self.load_servers()
        if not servers:
            return
            
        print(f"\n[{datetime.now()}] 开始采集服务器数据...")
        for server in servers:
            try:
                mc_server = JavaServer.lookup(server['ip'], timeout=2.0)
                status = mc_server.status()
                server_id = self._get_server_id(server['name'])
                
                self.db.execute(
                    "INSERT INTO stats (server_id, online) VALUES (?, ?)",
                    (server_id, status.players.online)
                )
                print(f"  ✓ {server['name']}: {status.players.online}人在线")
            except Exception as e:
                print(f"  ✕ {server['name']} 采集失败: {str(e)}")

    def generate_report(self, days=1):
        servers = self.load_servers()
        print(f"\n[{datetime.now()}] 生成报告...")
        
        print("\n=== 服务器状态报告 ===")
        for server in servers:
            c = self.db.execute('''
                SELECT 
                    MAX(online) as peak,
                    AVG(online) as avg_online,
                    COUNT(DISTINCT strftime('%Y-%m-%d', timestamp)) as days
                FROM stats
                JOIN servers ON servers.id = stats.server_id
                WHERE servers.name = ?
                  AND timestamp > datetime('now', ?)
                ''', (server['name'], f'-{days} days'))
            data = c.fetchone()
            
            print(f"\n{server['name']} ({server['ip']})")
            print(f"  峰值人数: {data[0] or 'N/A'}")
            print(f"  平均人数: {round(data[1], 1) if data[1] else 'N/A'}")
            print(f"  有效天数: {data[2]}")

    def detect_anomalies(self):
        servers = self.load_servers()
        print(f"\n[{datetime.now()}] 执行异常检测...")
        for server in servers:
            c = self.db.execute('''
                SELECT online 
                FROM stats
                JOIN servers ON servers.id = stats.server_id
                WHERE servers.name = ?
                ORDER BY timestamp DESC
                LIMIT 6
                ''', (server['name'],))
            
            data = [row[0] for row in c.fetchall()]
            if len(data) < 3:
                continue
                
            current = data[0]
            avg = mean(data[1:])
            std = stdev(data[1:]) if len(data) > 2 else 0
            
            if std == 0:
                threshold = avg * 1.5
            else:
                threshold = avg + 2*std
                
            if current > threshold:
                print(f"  ! {server['name']} 异常突增: {current} (平均 {round(avg,1)})")
            elif current < (avg - 2*std):
                print(f"  ! {server['name']} 异常下跌: {current} (平均 {round(avg,1)})")

    def clean_old_data(self):
        if CONFIG['never_delete_data']:
            print("\n[配置] 已启用永不删除数据模式")
            return
            
        print(f"\n[{datetime.now()}] 清理过期数据...")
        c = self.db.execute("DELETE FROM stats WHERE timestamp < datetime('now', '-30 days')")
        print(f"  已删除 {c.rowcount} 条记录")

    def plot_trend(self, server_names, days=7):
        plt.figure(figsize=(14, 8))
        colors = plt.cm.tab10.colors
        legend_handles = []
        
        for idx, name in enumerate(server_names):
            c = self.db.execute('''
                SELECT 
                    strftime('%Y-%m-%d %H:%M', timestamp) as time,
                    online
                FROM stats
                JOIN servers ON servers.id = stats.server_id
                WHERE servers.name = ?
                  AND timestamp > datetime('now', ?)
                ORDER BY timestamp
                ''', (name, f'-{days} days'))
            
            data = c.fetchall()
            if not data:
                print(f"  ✕ {name} 无数据")
                continue
                
            times = [row[0] for row in data]
            values = [row[1] for row in data]
            line, = plt.plot(times, values, 
                          color=colors[idx % len(colors)],
                          marker='o' if days < 2 else None,
                          linestyle='-')
            legend_handles.append(line)

        plt.title(f'服务器在线趋势（最近{days}天）')
        plt.xlabel('时间')
        plt.ylabel('在线人数')
        plt.xticks(rotation=45)
        plt.legend(legend_handles, server_names)
        plt.grid(True)
        plt.tight_layout()
        filename = f"trend_{days}d_{'_'.join(server_names)}.png"
        plt.savefig(filename)
        plt.close()
        print(f"趋势图已保存至 {filename}")

    def add_server(self, name, ip):
        try:
            JavaServer.lookup(ip, timeout=2).status()
            self.db.execute("INSERT INTO servers (name, ip) VALUES (?, ?)", (name, ip))
            print(f"成功添加服务器: {name} ({ip})")
        except sqlite3.IntegrityError:
            print("错误: 服务器名称或IP已存在")
        except Exception as e:
            if "timed out" in str(e):
                print(f"服务器验证失败: 连接超时（2秒未响应）")
            else:
                print(f"服务器验证失败: {str(e)}")

    def remove_server(self, name):
        c = self.db.execute("DELETE FROM servers WHERE name = ?", (name,))
        print(f"成功删除服务器: {name}") if c.rowcount > 0 else print("错误: 未找到该服务器")

    def delete_server_data(self, name):
        server_id = self._get_server_id(name)
        if not server_id:
            print("错误: 服务器不存在")
            return
            
        c = self.db.execute("DELETE FROM stats WHERE server_id = ?", (server_id,))
        print(f"已删除 {c.rowcount} 条{name}的数据记录")

    def real_time_query(self):
        """实时查询命令实现"""
        servers = self.load_servers()
        if not servers:
            print("\n当前没有监控任何服务器")
            return
            
        print(f"\n[{datetime.now()}] 实时查询结果：")
        for server in servers:
            try:
                mc_server = JavaServer.lookup(server['ip'], timeout=2.0)
                status = mc_server.status()
                print(f"  ✓ {server['name']}: {status.players.online}人在线")
            except Exception as e:
                if "timed out" in str(e):
                    print(f"  ✕ {server['name']} 查询超时（2秒未响应）")
                else:
                    print(f"  ✕ {server['name']} 查询失败: {str(e)}")

    def show_help(self):
        help_text = """
        可用命令：
        now        - 立即查询所有服务器的实时状态
        list       - 列出所有监控的服务器
        add <名称> <IP> - 添加新服务器
        remove <名称> - 移除服务器
        delete <名称> - 删除服务器的历史数据
        report [天数] - 生成统计报告
        trend <天数> <服务器1> [服务器2...] - 生成趋势图
        config     - 显示当前配置
        exit       - 退出程序
        """
        print(textwrap.dedent(help_text).strip())

    def run(self):
        print(r'''
        ----------------------------------------------------------------      
                      
        $$\      $$\        $$$$$$\         $$$$$$\        $$\      $$\ 
        $$\    $$$$ |      $$  __$$\       $$  __$$\       $%\    $$$$ |
        $$$$\  $$$$ |      $$ /  \__|      $$ /  \__|      $$$$\  $$$$ |
        $$\$$\$$ $$ |      $$ |            \$$$$$$\        $$\$$\$$ $$ |
        $$ \$$$  $$ |      $$ |             \____$$\       $$ \$$$  $$ |
        $$ |\$  /$$ |      $$ |  $$\       $$\   $$ |      $$ |\$  /$$ |
        $$ | \_/ $$ |      \$$$$$$  |      \$$$$$$  |      $$ | \_/ $$ |
        \__|     \__|       \______/        \______/       \__|     \__|
                      
        ----------------------------------------------------------------      
                                                                    
        Minecraft Server Monitor                    MADE BY chickenshout                                         
        ----------------------------------------------------------------  
        ''')
        print(f"数据存储: {CONFIG['db_name']}")
        print(f"采集间隔: {CONFIG['interval']}秒")
        print("输入 'help' 查看可用命令\n")
        print("> ", end='', flush=True)

        last_collect = time.time()
        last_report = datetime.now()
        last_clean = datetime.now()

        try:
            while self.running:
                # 处理输入命令
                self._process_input()

                # 定时数据采集
                if time.time() - last_collect >= CONFIG['interval']:
                    self.collect_data()
                    last_collect = time.time()

                # 每日报告
                now = datetime.now()
                if now.hour == 8 and now.minute < 5:
                    self.generate_report(1)
                    last_report = now

                # 每周清理
                if (now - last_clean).days >= 7:
                    self.clean_old_data()
                    last_clean = now

                # 每小时异常检测
                if now.minute == 0:  
                    self.detect_anomalies()

                time.sleep(0.1)

        except KeyboardInterrupt:
            self.running = False
        finally:
            print("\n监控已停止")

    def _process_input(self):
        while True:
            cmd = self.input_handler.get_cmd()
            if not cmd:
                break
            try:
                print(f"\n执行命令: {cmd}")
                self._execute_command(cmd)
            except Exception as e:
                print(f"命令执行错误: {str(e)}")
            print("> ", end='', flush=True)

    def _execute_command(self, cmd):
        if cmd == 'exit':
            self.running = False
        elif cmd == 'help':
            self.show_help()
        elif cmd == 'now':
            self.real_time_query()
        elif cmd == 'list':
            self._list_servers()
        elif cmd.startswith('add'):
            self._handle_add(cmd)
        elif cmd.startswith('remove'):
            self._handle_remove(cmd)
        elif cmd.startswith('delete'):
            self._handle_delete(cmd)
        elif cmd.startswith('report'):
            self._handle_report(cmd)
        elif cmd.startswith('trend'):
            self._handle_trend(cmd)
        elif cmd == 'config':
            self._show_config()
        else:
            print("未知命令，输入 help 查看帮助")

    def _list_servers(self):
        servers = self.load_servers()
        if not servers:
            print("\n当前没有监控任何服务器")
            return
        print("\n当前监控的服务器：")
        for s in servers:
            print(f"  {s['name']} ({s['ip']})")

    def _handle_add(self, cmd):
        parts = cmd.split()
        if len(parts) != 3:
            print("用法: add 服务器名称 IP地址")
            return
        name, ip = parts[1], parts[2]
        self.add_server(name, ip)

    def _handle_remove(self, cmd):
        parts = cmd.split()
        if len(parts) != 2:
            print("用法: remove 服务器名称")
            return
        self.remove_server(parts[1])

    def _handle_delete(self, cmd):
        parts = cmd.split()
        if len(parts) != 2:
            print("用法: delete 服务器名称")
            return
        self.delete_server_data(parts[1])

    def _handle_report(self, cmd):
        parts = cmd.split()
        try:
            days = int(parts[1]) if len(parts) > 1 else 1
            self.generate_report(days)
        except ValueError:
            print("错误: 天数必须为整数")

    def _handle_trend(self, cmd):
        parts = cmd.split()
        if len(parts) < 3:
            print("用法: trend 天数 服务器名称1 [服务器名称2...]")
            return
        try:
            days = int(parts[1])
            servers = parts[2:]
            valid_servers = [s['name'] for s in self.load_servers()]
            invalid = [s for s in servers if s not in valid_servers]
            if invalid:
                print(f"错误: 以下服务器不存在 - {', '.join(invalid)}")
                return
            self.plot_trend(servers, days)
        except ValueError:
            print("错误: 天数必须为整数")

    def _show_config(self):
        print("\n当前配置：")
        print(f"采集间隔: {CONFIG['interval']}秒")
        print(f"数据保留策略: {'永久保留' if CONFIG['never_delete_data'] else '自动清理30天前数据'}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Minecraft服务器监控')
    parser.add_argument('--never-delete', action='store_true', help='永不删除历史数据')
    args = parser.parse_args()

    monitor = MCMonitor(never_delete=args.never_delete)
    monitor.run()