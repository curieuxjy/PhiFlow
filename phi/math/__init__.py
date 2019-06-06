
from phi.math.base import *


def load_tensorflow():
    """
Internal function to register the TensorFlow backend.
This function is called automatically once a TFSimulation is instantiated.
    :return: True if TensorFlow could be imported, else False
    """
    try:
        import phi.math.tensorflow_backend as tfb
        for b in backend.backends:
            if isinstance(b, tfb.TFBackend): return True
        backend.backends.append(tfb.TFBackend())
        return True
    except BaseException as e:
        import logging
        logging.fatal("Failed to load TensorFlow backend. Error: %s" % e)
        print("Failed to load TensorFlow backend. Error: %s" % e)
        return False


from phi.math.nd import *
import container
from .initializers import *  # this replaces zeros_like (possibly more) and must be handled carefully