"""
Driver de comunicação com a placa UPUS-SKB25-V3.0 (25 canais, RS-485).

Protocolo real, baseado no documento técnico oficial do fabricante
(UPUS-SKB Series Technical Document V3.0+):

    Estrutura do quadro:
    [Header 0x55] [Type 0xA1] [Address] [Function] [DataLen] [Data...] [Parity]

    Parity = XOR de todos os bytes anteriores (Header xor Type xor
    Address xor Function xor DataLen xor cada byte de Data)

Comandos usados por este driver:
    E2 = Unlock (destravar um canal, ou 0x00 = todos)
    E1 = Write Channel Status (ligar/desligar energia de um canal)
    D1 = Read Channel Status (status de energia dos canais)
    D2 = Read Channel Signal Status (status do sensor/fim de curso)
    DD = Read MCU ID
"""

import threading
import time
# pyserial só é importado de verdade dentro de connect(), não aqui em
# cima. Assim, enquanto SIMULATE_BOARD=True no app.py, nem precisa
# instalar pyserial pra rodar o sistema.

# ---------------------------------------------------------------------------
# CONFIGURAÇÃO DA PORTA SERIAL (ajuste conforme seu conversor USB-RS485)
# ---------------------------------------------------------------------------
SERIAL_PORT = "COM7"            # Windows: "COM3", "COM7" etc. Linux: "/dev/ttyUSB0"
BAUDRATE = 9600                 # padrão de fábrica da placa (confirmado no manual)
TIMEOUT = 1.0

# Endereço da placa no barramento RS-485, definido pelo dip switch físico
# "Dialer" na própria placa (veja o manual: chave toda desligada = 1,
# toda ligada = 32).
BOARD_ADDRESS = 0x01

HEADER = 0x55
TYPE_CODE = 0xA1

# Função codes do protocolo
FUNC_UNLOCK = 0xE2
FUNC_WRITE_CHANNEL = 0xE1
FUNC_READ_CHANNEL_STATUS = 0xD1
FUNC_READ_CHANNEL_SIGNAL = 0xD2
FUNC_READ_MCU_ID = 0xDD


class LockerBoardError(Exception):
    pass


class LockerBoard:
    """
    Representa uma placa UPUS-SKB25-V3.0 de 25 canais.
    """

    def __init__(self, port=SERIAL_PORT, baudrate=BAUDRATE, address=BOARD_ADDRESS,
                 response_timeout=2.0, idle_silence=0.25):
        self.port = port
        self.baudrate = baudrate
        self.address = address
        self.lock = threading.Lock()
        self._ser = None
        # Configuráveis pela tela (ver painel ⚙️ → "Tempos de comunicação"):
        # response_timeout: quanto tempo esperar no total por uma resposta
        # idle_silence: quanto tempo de silêncio (sem receber bytes novos)
        # até considerar que a resposta terminou de chegar
        self.response_timeout = response_timeout
        self.idle_silence = idle_silence

    # ------------------------------------------------------------------
    # Conexão
    # ------------------------------------------------------------------
    def connect(self):
        try:
            import serial  # importado só aqui — não precisa estar instalado
                            # se você só for testar sem a placa conectada
        except ImportError:
            raise LockerBoardError(
                "pyserial não está instalado. Rode: pip install pyserial"
            )
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=TIMEOUT,
            )
        except serial.SerialException as e:
            raise LockerBoardError(f"Não foi possível abrir {self.port}: {e}")

    def disconnect(self):
        if self._ser and self._ser.is_open:
            self._ser.close()

    def is_connected(self):
        return self._ser is not None and self._ser.is_open

    # ------------------------------------------------------------------
    # Protocolo de baixo nível
    # ------------------------------------------------------------------
    @staticmethod
    def _parity(frame_bytes) -> int:
        """XOR de todos os bytes do quadro (sem incluir a própria paridade)."""
        x = 0
        for b in frame_bytes:
            x ^= b
        return x & 0xFF

    def _build_frame(self, function_code: int, data: bytes = b"") -> bytes:
        header = bytes([HEADER, TYPE_CODE, self.address, function_code, len(data)])
        frame_no_parity = header + data
        parity = self._parity(frame_no_parity)
        return frame_no_parity + bytes([parity])

    def _send_and_receive(self, function_code: int, data: bytes = b"", read_timeout=None) -> bytes:
        """
        Envia um comando e aguarda a resposta completa.

        A leitura acontece em duas fases:
          1) Espera o PRIMEIRO byte chegar, sem pressa nenhuma — a
             placa pode demorar até uns 300-400ms pra começar a
             responder (medido na prática), então não pode haver
             timeout de "silêncio" nessa fase, ou desistimos cedo
             demais mesmo quando a placa ia responder certinho.
          2) Depois que o primeiro byte chega, aí sim usamos silêncio
             (sem receber nada novo por um tempinho) pra saber que a
             resposta terminou — a placa não manda um "terminador"
             fixo no fim do quadro.
        """
        if not self.is_connected():
            raise LockerBoardError("Placa não conectada")

        frame = self._build_frame(function_code, data)
        timeout = read_timeout if read_timeout is not None else self.response_timeout

        try:
            with self.lock:
                self._ser.reset_input_buffer()
                self._ser.write(frame)

                buf = bytearray()
                deadline = time.time() + timeout

                # ---- Fase 1: espera o primeiro byte, sem timeout de ociosidade ----
                while time.time() < deadline and len(buf) == 0:
                    n = self._ser.in_waiting
                    if n:
                        buf.extend(self._ser.read(n))
                    else:
                        time.sleep(0.02)

                # ---- Fase 2: já tem pelo menos 1 byte, espera terminar por silêncio ----
                if buf:
                    idle_start = time.time()
                    while time.time() < deadline:
                        n = self._ser.in_waiting
                        if n:
                            buf.extend(self._ser.read(n))
                            idle_start = time.time()
                        else:
                            if time.time() - idle_start > self.idle_silence:
                                break
                            time.sleep(0.02)
        except LockerBoardError:
            raise
        except Exception as e:
            # Qualquer falha de baixo nível (adaptador USB desconectou,
            # driver travou, cabo solto no meio da operação, etc) cai
            # aqui. IMPORTANTE: nesse ponto o comando já pode ter sido
            # enviado e a placa já pode ter destravado fisicamente —
            # só a leitura da resposta que falhou. Por isso fechamos a
            # conexão, forçando reconectar limpo na próxima tentativa.
            try:
                self.disconnect()
            except Exception:
                pass
            raise LockerBoardError(
                f"Falha de comunicação serial durante o comando "
                f"(a placa pode já ter recebido o comando antes da falha): {e}"
            )

        if not buf:
            raise LockerBoardError(
                "A placa não respondeu. Verifique alimentação, fiação A+/B-, "
                f"e se o endereço ({self.address}) está correto."
            )

        return bytes(buf)

    @staticmethod
    def _extract_data(response: bytes) -> bytes:
        """
        Dado um quadro de resposta completo [55 A1 Addr Func Len Data... Parity],
        retorna só a porção de Data.
        """
        if len(response) < 6:
            raise LockerBoardError(f"Resposta muito curta: {response.hex()}")
        data_len = response[4]
        data = response[5:5 + data_len]
        if len(data) != data_len:
            raise LockerBoardError(
                f"Resposta incompleta: esperava {data_len} bytes de dado, "
                f"recebeu {len(data)}. Bruto: {response.hex()}"
            )
        return data

    # ------------------------------------------------------------------
    # API pública usada pelo resto do sistema
    # ------------------------------------------------------------------
    def open_channel(self, channel: int) -> bool:
        """
        Destrava a fechadura do canal indicado (comando E2 - Unlock).
        channel: 1 a 25 (0 destravaria todas, não usamos isso aqui de
        propósito, pra nunca abrir todos os armários por engano).
        """
        if not (1 <= channel <= 25):
            raise ValueError("Canal deve ser entre 1 e 25")

        response = self._send_and_receive(FUNC_UNLOCK, bytes([channel]))
        data = self._extract_data(response)

        if len(data) < 2:
            raise LockerBoardError(f"Resposta inesperada ao destravar: {response.hex()}")

        resp_channel, status = data[0], data[1]

        if status == 0xE5:
            raise LockerBoardError(f"Canal {channel} inválido (placa retornou E5)")

        # IMPORTANTE: apesar do nome "status", esse byte NÃO é um flag de
        # sucesso/falha do comando — é a leitura do sinal de FEEDBACK da
        # fechadura naquele instante (mesma codificação do D2: 0=on/alto,
        # 1=off/baixo), conforme o próprio exemplo do manual do
        # fabricante ("Status: 01 (off or low level)"). Esse valor varia
        # dependendo de a fechadura ter ou não o pino de feedback
        # cabeado, e não indica se o comando foi aceito.
        #
        # A placa já validou o comando (não retornou E5 = canal
        # inválido), então o comando FOI aceito e enviado à fechadura.
        # A confirmação real de que a porta abriu de verdade deve vir
        # da leitura do sensor de fim de curso (D2), não deste byte —
        # veja a Opção 1 de notificação por sensor no sistema.
        return True

    def set_channel_power(self, channel: int, on: bool) -> bool:
        """Liga/desliga a energia de um canal (comando E1)."""
        if not (1 <= channel <= 25):
            raise ValueError("Canal deve ser entre 1 e 25")

        response = self._send_and_receive(
            FUNC_WRITE_CHANNEL, bytes([channel, 1 if on else 0])
        )
        data = self._extract_data(response)

        if len(data) < 2:
            raise LockerBoardError(f"Resposta inesperada: {response.hex()}")

        _, status = data[0], data[1]
        if status == 0xE5:
            raise LockerBoardError(f"Canal {channel} inválido (placa retornou E5)")
        return True

    def get_status(self) -> dict:
        """
        Lê o status de energia (ligado/desligado) de todos os 25 canais
        (comando D1, canal 0 = todos).
        Retorna {canal: 'on'/'off'}.
        """
        response = self._send_and_receive(FUNC_READ_CHANNEL_STATUS, bytes([0x00]))
        data = self._extract_data(response)

        if len(data) < 2:
            raise LockerBoardError(f"Resposta inesperada: {response.hex()}")

        status_bytes = data[1:]  # data[0] é o "channel NO." (0 = todos)
        bits = "".join(f"{b:08b}" for b in status_bytes)

        result = {}
        for i in range(25):
            result[i + 1] = "on" if i < len(bits) and bits[i] == "1" else "off"
        return result

    def get_signal_status(self) -> dict:
        """
        Lê o status do sinal/sensor (fim de curso, sensor de porta, etc)
        de todos os 25 canais (comando D2). 0 = conectado/alto, 1 =
        desconectado/baixo — atenção que é o INVERSO do get_status().
        """
        response = self._send_and_receive(FUNC_READ_CHANNEL_SIGNAL, bytes([0x00]))
        data = self._extract_data(response)

        if len(data) < 2:
            raise LockerBoardError(f"Resposta inesperada: {response.hex()}")

        status_bytes = data[1:]
        bits = "".join(f"{b:08b}" for b in status_bytes)

        result = {}
        for i in range(25):
            result[i + 1] = "connected" if i < len(bits) and bits[i] == "0" else "disconnected"
        return result

    def get_channel_signal(self, channel: int) -> str:
        """
        Lê o sinal/sensor de UM único canal (mais rápido que ler todos
        os 25 quando só precisa confirmar um armário específico, ex:
        depois de destravar, pra saber se a porta foi realmente aberta).
        Retorna 'connected' ou 'disconnected'.
        """
        if not (1 <= channel <= 25):
            raise ValueError("Canal deve ser entre 1 e 25")

        response = self._send_and_receive(FUNC_READ_CHANNEL_SIGNAL, bytes([channel]))
        data = self._extract_data(response)

        if len(data) < 2:
            raise LockerBoardError(f"Resposta inesperada: {response.hex()}")

        return "connected" if data[1] == 0x00 else "disconnected"

    def read_mcu_id(self) -> str:
        """Lê o número de série do processador da placa (comando DD)."""
        response = self._send_and_receive(FUNC_READ_MCU_ID)
        data = self._extract_data(response)
        return data.hex().upper()


# ---------------------------------------------------------------------------
# Modo de teste manual — rode "python board_driver.py" pra testar a
# comunicação bruta antes de ligar isso na interface web.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    board = LockerBoard()
    try:
        board.connect()
        print(f"Conectado em {board.port}")

        mcu_id = board.read_mcu_id()
        print(f"ID da placa: {mcu_id}")

        ch = int(input("Digite o número do canal para destravar (1-25): "))
        ok = board.open_channel(ch)
        print("Destravado com sucesso!" if ok else "A placa respondeu, mas não confirmou destravamento.")
    except LockerBoardError as e:
        print("Erro:", e)
    finally:
        board.disconnect()
